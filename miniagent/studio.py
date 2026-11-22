"""Studio service: groups, running tasks and the requester's side of the chat.

A room's ordinary conversation is read-only. The addressed member interprets
each message using that room's history, current task and published deliveries.
Explicit work and changes reach TeamTask through the same service as CLI dispatch.
"""

from __future__ import annotations

import io
import json
import re
import threading
import uuid
import zipfile
from collections import deque
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from .config import Settings
from .providers import DeepSeekProvider, Provider
from .store import GroupStore, Store, Member, now
from .roles import Roles
from .attachments import Uploads
from .assistant_room import workspace_evidence, progress, read_dispatch, legacy_dispatch_origins
from . import dispatch_notices
from .folders import bind, sync
from .evaluation import validate, evaluate, snapshot, task_changes
from .provider_http import ProviderError
from .room_turn import decide, read_excerpt, room_evidence
from .team import HumanChannel, TeamTask

ProviderFactory = Callable[[Settings], Provider]


def deepseek_factory(settings: Settings) -> Provider:
    return DeepSeekProvider(api_key=settings.api_key, base_url=settings.base_url, model=settings.model,
                            thinking=settings.thinking, reasoning_effort=settings.reasoning_effort,
                            max_output_tokens=settings.max_output_tokens, timeout=settings.request_timeout_seconds,
                            deadline_seconds=settings.request_deadline_seconds, max_retries=settings.max_retries)


class StudioError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Running:
    task_id: str
    primary: str
    channel: HumanChannel
    cancel: threading.Event
    thread: threading.Thread | None = None
    activity: deque = field(default_factory=lambda: deque(maxlen=3000))
    activity_seq: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


def mention(text: str, store: GroupStore) -> tuple[str | None, str]:
    """``@id`` (or ``@Name``) at the very start addresses one member."""
    match = re.match(r"\s*@([^\s,:：，]+)[\s,:：，]*", text)
    if not match:
        return None, text
    token = match.group(1).casefold()
    for member in store.group.members:
        if token in {member.id.casefold(), member.name.casefold()}:
            return member.id, text[match.end():].strip() or text
    raise StudioError(f"@{match.group(1)} is not a member of this group.")


@dataclass(frozen=True)
class ObservedTask:
    group_id: str
    run: Running
    question: dict | None


class Studio:
    def __init__(self, settings: Settings, provider_factory: ProviderFactory | None = None) -> None:
        self.settings = settings
        self.env_file = Path('.env').resolve()
        self.roles = Roles(settings.runs_dir)
        self.uploads = Uploads(settings.runs_dir)
        self._closed = False
        self.store = Store(settings.runs_dir)
        self.provider_factory = provider_factory or deepseek_factory
        self._running: dict[str, Running] = {}
        self._lock = threading.RLock()
        self._dispatch_changed = threading.Condition(self._lock)
        self._folder_sync_lock = threading.Lock()

    # -- groups -----------------------------------------------------------
    def create_group(self, name: str, members: list[dict], lead: str, mode: str = "interactive", *, role_ids=(), kind="group", created_by=None) -> dict:
        try:
            if not isinstance(role_ids, (list, tuple)) or len(set(role_ids)) != len(role_ids):
                raise ValueError("Invalid library roles")
            with self._lock:
                selected = [self.roles.get(r) for r in role_ids]
                notes = {m["id"]:m.get("note","") for m in members}
                roster = [m for m in members if m["id"] not in role_ids]
                roster.extend({**row,"note":notes.get(row["id"],"")} for row in selected)
                source = self.group(created_by["group_id"]).group if created_by else None
                from_assistant = source is not None and source.kind == "assistant"
                store = self.store.create(name, roster, lead, mode, role_ids=list(role_ids), kind=kind,
                                          created_by=created_by, pinned=from_assistant and kind=="group",
                                          folder=source.folder if from_assistant else "")
                self._bind_matching_roles(store.group)
                self.store.update_group(store)
                dispatch_notices.group_created(self, store)
        except (ValueError, KeyError) as exc:
            raise StudioError(str(exc)) from None
        return self.summary(store)

    def _bind_matching_roles(self, group):
        if group.kind == "assistant":
            return
        existing = {row["id"]: row for row in self.roles.list()}
        for member in group.members:
            if member.id in group.role_ids:
                continue
            row = existing.get(member.id)
            if row is None:
                self.roles.save(member.__dict__)
            elif (member.name, member.prompt) != (row["name"], row["prompt"]):
                continue  # A different local role may share a short ID.
            group.role_ids.append(member.id)

    def summary(self, store: GroupStore) -> dict:
        running = self._running.get(store.group.id)
        tasks = store.tasks()
        return {**store.group.to_dict(), "workspace_path":store.group.folder or str(store.project.root.resolve()),
                "running": bool(running),
                "waiting_for_you": bool(running and running.channel.pending()),
                "last_activity": (store.root/"chat.jsonl").stat().st_mtime, "tasks": len(tasks), "last_status": tasks[-1]["status"] if tasks else None}

    def groups(self) -> list[dict]:
        return [self.summary(self.store.get(g.id)) for g in self.store.groups()]

    def group(self, group_id: str) -> GroupStore:
        try:
            return self.store.get(group_id)
        except KeyError:
            raise StudioError("Unknown group.", 404) from None

    def update_group(self, group_id: str, changes: dict) -> dict:
        store = self.group(group_id)
        with self._lock:
            if group_id in self._running and ("folder" in changes or changes.get("archived")):
                raise StudioError("Wait for the task before rebinding or archiving its workspace.", 409)
            group = store.group
            original = group.to_dict()
            old_project, old_state = store.project, store.folder_state
            try:
                if "name" in changes:
                    group.name = str(changes["name"]).strip()
                if "mode" in changes:
                    group.mode = changes["mode"]
                if "members" in changes:
                    group.members = [Member(id=m["id"].strip(), name=(m.get("name") or m["id"]).strip(),
                                            prompt=(m.get("prompt") or "").strip(), note=m.get("note", "")) for m in changes["members"]]
                for key in ("archived","pinned"):
                    if key in changes:
                        if type(changes[key]) is not bool:
                            raise ValueError("Expected a boolean")
                        setattr(group,key,changes[key])
                if "role_ids" in changes:
                    group.role_ids = list(changes["role_ids"])
                    members = {m.id:m for m in group.members}
                    for key in group.role_ids:
                        row = self.roles.get(key)
                        members[key] = Member(key,row["name"],row["prompt"], members[key].note if key in members else "")
                    group.members = list(members.values())
                group.role_ids = [r for r in group.role_ids if r in {m.id for m in group.members}]
                if "folder" in changes:
                    if changes["folder"]:
                        bind(store, changes["folder"])
                    else:
                        group.folder = ""
                if "lead" in changes:
                    group.lead = changes["lead"]
                group.validate()
                if "members" in changes or "role_ids" in changes:
                    self._bind_matching_roles(group)
                if group.folder != original["folder"]:
                    store.select_project()
                self.store.update_group(store)
            except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
                restored = type(group).from_dict(original)
                store.group.__dict__.update(restored.__dict__)
                store.project, store.folder_state = old_project, old_state
                raise StudioError(f"Invalid change: {exc}") from None
            old_names = {m["id"]:m["name"] for m in original["members"]}
            new_names = {m.id:m.name for m in group.members}
            store.append("group_updated", name=group.name, mode=group.mode, lead=group.lead,
                         members=[m.__dict__ for m in group.members],
                         joined=[name for rid,name in new_names.items() if rid not in old_names],
                         left=[name for rid,name in old_names.items() if rid not in new_names])
        return self.summary(store)

    def change_members(self, group_id, add_role_ids=(), remove_member_ids=(), lead=None):
        """Apply a membership delta against the current roster under the same lock as UI edits."""
        with self._lock:
            store = self.group(group_id)
            self._require_available(store)
            group = store.group
            if group.kind != "group":
                raise StudioError("Choose an existing group chat for membership changes.")
            for ids in (add_role_ids, remove_member_ids):
                if not isinstance(ids, (list, tuple)) or not all(isinstance(i, str) and i for i in ids):
                    raise StudioError("Member IDs must be a list of role IDs.")
            if not add_role_ids and not remove_member_ids and lead is None:
                raise StudioError("Specify members to add or remove, or a new lead.")
            add, remove = set(add_role_ids), set(remove_member_ids)
            if add & remove:
                raise StudioError("A member cannot be added and removed in the same change.")
            members = {m.id:m for m in group.members if m.id not in remove}
            bound = [rid for rid in group.role_ids if rid not in remove]
            for rid in dict.fromkeys(add_role_ids):
                role = self.roles.get(rid)
                local = members.get(rid)
                if local and rid not in bound and (local.name, local.prompt) != (role["name"], role["prompt"]):
                    raise StudioError(f"A different local member already uses {rid}.")
                if not local:
                    members[rid] = Member(rid, role["name"], role["prompt"])
                if rid not in bound:
                    bound.append(rid)
            next_lead = group.lead if lead is None else lead
            if next_lead not in members:
                raise StudioError("Specify a remaining member as lead when removing the current lead.")
            if (list(members) == [m.id for m in group.members] and bound == group.role_ids and next_lead == group.lead):
                return self.summary(store)
            return self.update_group(group_id, {"members":[m.__dict__ for m in members.values()],
                                                "role_ids":bound, "lead":next_lead})

    # -- messages ---------------------------------------------------------
    def post(self, group_id, text, *, attachments=(), options=None, queue=False, dispatched_by=None):
        store = self.group(group_id)
        if not isinstance(text, str) or len(text) > 60_000:
            raise StudioError("Expected a message of at most 60000 characters")
        if not isinstance(attachments, (list, tuple)) or len(attachments) > 20:
            raise StudioError("Expected up to 20 attachments")
        refs = [self.uploads.ref(ref) for ref in attachments]
        if not text.strip() and not refs:
            raise StudioError("The message is empty.")
        if self.settings.api_key and self.settings.api_key in text:
            raise StudioError("Message contains the configured API key.")
        options = {} if options is None else options
        settings, mode = self._task_settings(store, options)
        with store.turn_lock, ExitStack() as turn_scope:
            with self._lock:
                self._require_available(store)
                current = self._running.get(group_id)
                assistant = store.group.kind == "assistant"
                target, body = (None, text) if assistant else mention(text, store)
                question = current.channel.pending() if current else None
                member_ids = {m.id for m in store.group.members}
                actor = target or (question["agent"] if question and question["agent"] in member_ids else store.group.lead)
            if queue:
                return self._submit(store, text, body, target or store.group.lead, refs, options, settings, mode,
                                    dispatched_by=dispatched_by)

            stream = turn_scope.enter_context(store.previews.room_turn(actor, text, refs))
            active = {"id":current.task_id, "primary":current.primary} if current else None
            context = room_evidence(store, actor, active, question, refs, self.roles)
            # Keep the identity of each observed execution and question until the model returns.
            recipients = {current.task_id:ObservedTask(group_id,current,question)} if current else {}
            children = {}
            if assistant:
                context.update(workspace_evidence(self, store))
                with self._lock:
                    for item in context["dispatches"] + context["earlier_dispatches"]:
                        tid, gid = item["id"], item["group_id"]
                        children[tid] = item
                        child = self._running.get(gid)
                        if child and child.task_id == tid:
                            recipients[tid] = ObservedTask(gid, child, child.channel.pending())
                context["action_targets"] = {
                    tid:{"group_id":observed.group_id, "question":observed.question, "activity":progress(observed.run)}
                    for tid,observed in recipients.items()}
            try:
                def read(args):
                    child = children.get(args.get("task_id")) if isinstance(args.get("task_id"), str) else None
                    if child:
                        excerpt = read_dispatch(self, child, args)
                    else:
                        excerpt = read_excerpt(store, self.uploads, context, args)
                    return excerpt.replace(settings.api_key, "[redacted]") if settings.api_key else excerpt
                turn = decide(self.provider_factory(settings), context, text, read, on_stream=stream)
            except (ProviderError, ValueError) as exc:
                raise StudioError(f"无法处理会话消息：{exc}", 503) from None

            with self._lock:
                self._require_available(store)
                if turn.action == "start_room_task":
                    return self._submit(store, text, body, target or store.group.lead, refs, options, settings, mode)
                if turn.action == "change_group_members" and assistant:
                    try:
                        updated = self.change_members(**turn.membership)
                        names = "、".join(m["name"] for m in updated["members"])
                        answer = f"「{updated['name']}」当前成员为 {names}，负责人是 @{updated['lead']}。"
                    except (StudioError, ValueError) as exc:
                        answer = f"群成员调整未完成：{exc}"
                elif turn.action == "reply":
                    answer = turn.text
                else:
                    tid = turn.task_id or (current.task_id if current else None)
                    route = self._deliver_turn(recipients.get(tid), turn.action, text, refs)
                    if route != "stale":
                        if tid in children:
                            verb = "答复" if route == "answer" else "修改要求"
                            self._record_reply(store, actor, text, refs,
                                               f"已将{verb}转交给「{children[tid]['group_name']}」的任务 {tid}。")
                        return {"route":route, "task_id":tid}
                    answer = "刚才的问题或执行已经结束。这条消息没有转交给其他任务。"
                if settings.api_key:
                    answer = answer.replace(settings.api_key, "[redacted]")
                return self._record_reply(store, actor, text, refs, answer)

    @staticmethod
    def _record_reply(store, actor, text, refs, answer):
        message = store.append("user_message", text=text, addressed=actor, attachments=refs)
        store.append("room_reply", text=answer, agent=actor, in_reply_to=message["seq"])
        return {"route":"reply", "text":answer}

    def _deliver_turn(self, observed, action, text, refs):
        if not observed or self._running.get(observed.group_id) is not observed.run:
            return "stale"
        answering = action == "answer_open_question"
        if answering and not observed.question:
            return "stale"
        # receive also checks cancellation, completion and the exact pending question under its lock.
        return observed.run.channel.receive(text, attachments=refs, steer_only=not answering,
                                            answer_to=observed.question["id"] if answering else None)

    def _require_available(self, store):
        if self._closed or store.group.archived or not store.root.is_dir():
            raise StudioError("Conversation unavailable", 409)

    def _submit(self, store, text, body, primary, refs, options, settings, mode, *, dispatched_by=None):
        group_id = store.group.id
        with self._lock:
            self._require_available(store)
            store.group.member(primary)
            running = self._running.get(group_id)
            options = {**options, "thinking":settings.thinking, "interaction_mode":mode,
                       "member_max_steps":settings.limits.max_steps, "lead_max_steps":settings.team.lead_max_steps,
                       "max_delegations":settings.team.max_delegations}
            tid = "t-" + uuid.uuid4().hex[:12]
            if running:
                event = store.append("queued_message", task_id=tid, text=text)
            else:
                event = store.append("user_message", task_id=tid, text=text, attachments=refs, addressed=primary)
            record = {"id":tid, "group_id":group_id, "room_kind":store.group.kind, "status":"queued", "created_at":now(), "primary":primary,
                      "text":body, "trigger_seq":event["seq"], "options":options, "active":[]}
            if dispatched_by:
                record["dispatched_by"] = dispatched_by
            if running:
                record["queued_message"] = {"text":text,"attachments":refs,"addressed":primary}
            store.save_task(record)
            dispatch_notices.task_receipt(self, store, record)
            if running:
                return {"route":"queued","task_id":tid}
            self._start(store, record)
            return {"route":"task", "task_id":tid}

    def _task_settings(self, store, options):
        if not isinstance(options, dict) or options.keys() - {
                "thinking","interaction_mode","member_max_steps","lead_max_steps","max_delegations","checks"}:
            raise StudioError("Invalid task options")
        mode = options.get("interaction_mode", store.group.mode)
        if mode not in ("interactive","autonomous"):
            raise StudioError("Unknown interaction mode")
        validate(options.get("checks", []))
        base = self.settings
        settings = replace(base, thinking=options.get("thinking", base.thinking),
                           limits=replace(base.limits, max_steps=options.get("member_max_steps", base.limits.max_steps)),
                           team=replace(base.team, **{k:options[k] for k in ("max_delegations","lead_max_steps") if k in options}))
        return settings, mode

    def _start(self, store, record):
        tid, primary = record["id"], record["primary"]
        try:
            settings, mode = self._task_settings(store, record["options"])
            provider = self.provider_factory(settings)
            with self._folder_sync_lock:
                warnings = sync(store, importing=True, key=settings.api_key)
            for warning in warnings:
                store.append("system",task_id=tid,text=warning)
            members = []
            for member in store.group.members:
                if member.id in store.group.role_ids:
                    role = self.roles.get(member.id)
                    member = Member(role["id"],role["name"],role["prompt"],member.note)
                if member.note:
                    member = replace(member,prompt=member.prompt+"\nGroup note: "+member.note)
                members.append(member)
            if "queued_message" in record:
                event = next((e for e in store.chat() if e["kind"] == "user_message" and e.get("task_id") == tid), None)
                if event is None:
                    event = store.append("user_message", task_id=tid, **record["queued_message"])
                record["trigger_seq"] = event["seq"]
                del record["queued_message"]
                store.save_task(record)
            cancel = threading.Event()
            running = Running(tid, primary, HumanChannel(store, tid, cancel.is_set), cancel)
            task = TeamTask(store, provider, settings, task_id=tid, text=record["text"], primary=primary,
                            trigger_seq=record["trigger_seq"], channel=running.channel, should_cancel=cancel.is_set,
                            on_activity=lambda e: self._activity(running, e))
            task.group = replace(store.group, members=members, role_ids=list(store.group.role_ids), mode=mode)
            task.dispatched_by = record.get("dispatched_by")
            task.interactive = mode == "interactive"
            task.studio = self
            task.options = record["options"]
            task.created_at = record["created_at"]
            running.thread = threading.Thread(target=self._run, args=(store.group.id, task, running),
                                              name=f"task-{tid}", daemon=True)
            self._running[store.group.id] = running
            running.thread.start()
        except Exception as exc:
            self._running.pop(store.group.id, None)
            record.update(status="failed", finished_at=now(), error=str(exc))
            store.save_task(record)
            store.append("task_finished", task_id=tid, status="failed", error=str(exc))
            dispatch_notices.task_outcome(self, store, record)
            self._dispatch_changed.notify_all()
            raise StudioError(str(exc), 503) from None

    def _advance(self, store):
        if self._closed or store.group.archived or store.group.id in self._running:
            return
        for record in store.tasks():
            if record.get("status") == "queued":
                try:
                    self._start(store, record)
                    break
                except StudioError:
                    continue

    def restore_queue(self):
        with self._lock:
            groups = self.store.groups()
            legacy = {}
            assistant_ids = {group.id for group in groups if group.kind == "assistant"}
            for gid in assistant_ids:
                legacy.update(legacy_dispatch_origins(self.group(gid)))
            for g in groups:
                store = self.group(g.id)
                # Older releases wrote pinned=False for every room. An update in those
                # releases did not record which preferences were changed, so respect it.
                if (g.kind == "group" and (g.created_by or {}).get("group_id") in assistant_ids
                        and not g.pinned and not g.archived and not any(e["kind"] == "group_updated" for e in store.chat())):
                    store.group.pinned = True
                    self.store.update_group(store)
                dispatch_notices.group_created(self, store)
                active = self._running.get(g.id)
                for task in store.tasks():
                    if active and active.task_id == task["id"]:
                        continue
                    if task["status"] == "running":
                        task.update(status="interrupted", finished_at=now(), active=[])
                        store.save_task(task)
                        store.append("task_finished",task_id=task["id"],status="interrupted")
                    origin = legacy.get(task["id"])
                    dispatch_notices.task_receipt(self, store, task, legacy_origin=origin)
                    dispatch_notices.task_outcome(self, store, task, legacy_origin=origin)
                self._advance(store)

    def assistant(self):
        with self._lock:
            for g in self.store.groups():
                if g.kind == "assistant":
                    return self.summary(self.group(g.id))
            return self.create_group("工作室助手", [{"id":"assistant","name":"工作室助手",
                "prompt":"Help the user in conversation. You can create roles, organize groups and dispatch work through Studio tools."}],
                "assistant", kind="assistant")

    def dispatch_status(self, task_id):
        store, task = self.find_task(task_id)
        return {"id":task_id,"group_id":store.group.id,"status":task["status"],
                "final_answer":task.get("final_answer",""),"changes":task_changes(store,task_id)}

    def await_dispatch(self, gid, tid, source_id, parent_id):
        """Wait for the actual child outcome, including snapshot/evaluation, without model polling."""
        target = self.group(gid)
        with self._dispatch_changed:
            while True:
                parent = self._running.get(source_id)
                if self._closed or not parent or parent.task_id != parent_id or parent.cancel.is_set():
                    raise StudioError("The dispatching task was stopped.")
                task = target.load_task(tid)
                child = self._running.get(gid)
                if task["status"] not in ("queued","running") and (not child or child.task_id != tid):
                    return self.dispatch_status(tid)
                self._dispatch_changed.wait()

    def find_task(self, tid):
        if not re.fullmatch(r"t-[a-f0-9]{12}", tid):
            raise StudioError("Unknown task",404)
        for group in self.store.groups():
            store = self.group(group.id)
            if (store.root/"tasks"/tid/"task.json").is_file():
                return store, store.load_task(tid)
        raise StudioError("Unknown task",404)

    def task_detail(self, tid, *, include_activity=True):
        store, record = self.find_task(tid)
        trace = store.task_dir(tid)/"trace.jsonl"
        activity = [json.loads(line) for line in trace.read_text().splitlines()] if include_activity and trace.exists() else []
        events = [e for e in store.chat() if e.get("task_id") == tid]
        return {"task":record,"events":events,"activity":activity,"changes":task_changes(store,tid),
                "has_snapshot":(store.root/"tasks"/tid/"artifacts.zip").is_file(),
                "diagnostics":[e for e in activity if e.get("type") in ("loop_nudge","loop_stuck","error")
                               or (e.get("type")=="end" and e.get("status") not in ("completed","dispatched"))]}

    def trash(self, gid):
        with self._lock:
            store = self.group(gid)
            if gid in self._running or store.group.kind == "assistant":
                raise StudioError("Cannot move this conversation to trash",409)
            target = self.store.root.parent/"trash"/gid
            target.parent.mkdir(exist_ok=True)
            store.root.rename(target)
            self.store._open.pop(gid, None)
            return {"trashed":gid}


    def _run(self, group_id: str, task: TeamTask, running: Running) -> None:
        store = task.store
        try:
            result = task.run()
            try:
                snapshot(store, task.task_id)
                result["evaluation"] = evaluate(store, task.options.get("checks", []), task.settings)
                if store.group.folder:
                    published = {c["path"] for c in task_changes(store, task.task_id)}
                    with self._folder_sync_lock:
                        result["folder_warnings"] = sync(store, importing=False, key=task.settings.api_key,
                                                         published=published)
                else:
                    result["folder_warnings"] = []
            except (ValueError, OSError) as exc:
                result["delivery_warning"] = str(exc)
            store.save_task(result)
            for warning in result.get("folder_warnings", []):
                store.append("system", task_id=task.task_id, text=warning)
        except Exception as exc:  # noqa: BLE001 - recorded in the chat, never crashes the server
            store.append("task_finished", task_id=task.task_id, status="failed", error=f"{type(exc).__name__}: {exc}")
            try:
                record = store.load_task(task.task_id)
                record.update(status="failed", error=f"{type(exc).__name__}: {exc}", active=[])
                store.save_task(record)
            except (OSError, ValueError):
                pass
        finally:
            with self._lock:
                if self._running.get(group_id) is running:
                    try:
                        dispatch_notices.task_outcome(self, store, store.load_task(task.task_id))
                    finally:
                        del self._running[group_id]
                        self._dispatch_changed.notify_all()
                        self._advance(store)

    def _activity(self, running: Running, event: dict) -> None:
        with running.lock:
            if event.get("type") == "delta" and running.activity and running.activity[-1].get("type") == "delta" \
                    and running.activity[-1].get("stream_id") == event.get("stream_id") \
                    and event.get("phase") == running.activity[-1].get("phase") == "delta":
                running.activity[-1]["text"] += event.get("text", "")
                running.activity[-1]["seq"] = running.activity_seq = running.activity_seq + 1
                return
            running.activity_seq += 1
            running.activity.append({**event, "seq": running.activity_seq})

    def cancel(self, group_id: str) -> dict:
        with self._lock:
            running = self._running.get(group_id)
            if running is None:
                return {"cancelled": False}
            running.cancel.set()
            self._dispatch_changed.notify_all()
            self.group(group_id).append("system", text="The requester stopped the task.", task_id=running.task_id)
            return {"cancelled": True, "task_id": running.task_id}

    def wait(self, group_id: str, timeout: float | None = None) -> None:
        running = self._running.get(group_id)
        if running and running.thread:
            running.thread.join(timeout)

    # -- views ------------------------------------------------------------
    def state(self, group_id: str, after: int = 0, activity_after: int = 0) -> dict:
        store = self.group(group_id)
        running = self._running.get(group_id)
        live = None
        if running is not None:
            with running.lock:
                activity = [dict(e) for e in running.activity if e["seq"] > activity_after]
                cursor = running.activity_seq
            try:
                record = store.load_task(running.task_id)
            except (OSError, ValueError):
                record = {}
            live = {"task_id": running.task_id, "primary": running.primary, "active": record.get("active", []),
                    "question": running.channel.pending(), "activity": activity[-400:], "activity_seq": cursor}
        return {"group": self.summary(store), "events": store.chat(after), "live": live}

    def files(self, group_id: str) -> dict:
        store = self.group(group_id)
        return {"files": store.project.files(), "history": store.project.history(100)}

    def read_file(self, group_id: str, rel: str) -> Path:
        store = self.group(group_id)
        try:
            path = store.project.resolve(rel)
        except ValueError:
            raise StudioError("Invalid path.", 400) from None
        if not path.is_file():
            raise StudioError("File not found.", 404)
        return path

    def archive(self, group_id: str) -> bytes:
        store = self.group(group_id)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for item in store.project.files():
                archive.write(store.project.root / item["path"], item["path"])
        return buffer.getvalue()

    def tasks(self, group_id: str) -> list[dict]:
        return self.group(group_id).tasks()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._dispatch_changed.notify_all()
            runs = list(self._running.values())
        for running in runs:
            running.cancel.set()
        for running in runs:
            if running.thread:
                running.thread.join(10)
