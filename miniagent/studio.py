"""Studio service: groups, running tasks and the requester's side of the chat.

One task runs per group at a time, on its own thread. A requester message is
routed by state, the way a person would expect in a group chat:

* an agent is waiting for an answer → the message is the answer;
* a task is running → the message is delivered to the working agents at
  their next step (steering);
* the group is idle → the message starts a new task. ``@member`` at the start
  addresses that member directly instead of the lead.
"""

from __future__ import annotations

import io
import re
import threading
import uuid
import zipfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import Settings
from .providers import DeepSeekProvider, Provider
from .store import GroupStore, Store
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


class Studio:
    def __init__(self, settings: Settings, provider_factory: ProviderFactory | None = None) -> None:
        self.settings = settings
        self.store = Store(settings.runs_dir)
        self.provider_factory = provider_factory or deepseek_factory
        self._running: dict[str, Running] = {}
        self._lock = threading.RLock()

    # -- groups -----------------------------------------------------------
    def create_group(self, name: str, members: list[dict], lead: str, mode: str = "interactive") -> dict:
        try:
            store = self.store.create(name, members, lead, mode)
        except (ValueError, KeyError) as exc:
            raise StudioError(str(exc)) from None
        return self.summary(store)

    def summary(self, store: GroupStore) -> dict:
        running = self._running.get(store.group.id)
        tasks = store.tasks()
        return {**store.group.to_dict(), "running": bool(running),
                "waiting_for_you": bool(running and running.channel.pending()),
                "tasks": len(tasks), "last_status": tasks[-1]["status"] if tasks else None}

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
            if group_id in self._running:
                raise StudioError("The group is working on a task; change it when the task has finished.", 409)
            group = store.group
            original = group.to_dict()
            try:
                if "name" in changes:
                    group.name = str(changes["name"]).strip()
                if "mode" in changes:
                    group.mode = changes["mode"]
                if "members" in changes:
                    from .store import Member
                    group.members = [Member(id=m["id"].strip(), name=(m.get("name") or m["id"]).strip(),
                                            prompt=(m.get("prompt") or "").strip()) for m in changes["members"]]
                if "lead" in changes:
                    group.lead = changes["lead"]
                self.store.update_group(store)
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                restored = type(group).from_dict(original)
                store.group.__dict__.update(restored.__dict__)
                raise StudioError(f"Invalid change: {exc}") from None
            store.append("group_updated", name=group.name, mode=group.mode, lead=group.lead,
                         members=[m.__dict__ for m in group.members])
        return self.summary(store)

    # -- messages ---------------------------------------------------------
    def post(self, group_id: str, text: str) -> dict:
        text = (text or "").strip()
        if not text:
            raise StudioError("The message is empty.")
        if len(text) > 60_000:
            raise StudioError("The message is longer than 60,000 characters.")
        store = self.group(group_id)
        with self._lock:
            running = self._running.get(group_id)
            if running is not None:
                route = running.channel.receive(text)
                return {"route": route, "task_id": running.task_id}
            primary, body = mention(text, store)
            primary = primary or store.group.lead
            try:
                provider = self.provider_factory(self.settings)
            except Exception as exc:  # noqa: BLE001 - shown to the requester
                raise StudioError(f"The model is not configured: {exc}", 503) from None
            event = store.append("user_message", text=text, addressed=primary if primary != store.group.lead else None)
            task_id = "t-" + uuid.uuid4().hex[:12]
            cancel = threading.Event()
            running = Running(task_id, primary, HumanChannel(store, task_id, cancel.is_set), cancel)
            task = TeamTask(store, provider, self.settings, task_id=task_id, text=body, primary=primary,
                            trigger_seq=event["seq"], channel=running.channel, should_cancel=cancel.is_set,
                            on_activity=lambda e, r=running: self._activity(r, e))
            running.thread = threading.Thread(target=self._run, args=(group_id, task, running),
                                              name=f"task-{task_id}", daemon=True)
            self._running[group_id] = running
            running.thread.start()
        return {"route": "task", "task_id": task_id}

    def _run(self, group_id: str, task: TeamTask, running: Running) -> None:
        store = task.store
        try:
            task.run()
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
                    del self._running[group_id]

    def _activity(self, running: Running, event: dict) -> None:
        with running.lock:
            if event.get("type") == "delta" and running.activity and running.activity[-1].get("type") == "delta" \
                    and running.activity[-1].get("stream_id") == event.get("stream_id"):
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
            runs = list(self._running.values())
        for running in runs:
            running.cancel.set()
        for running in runs:
            if running.thread:
                running.thread.join(10)
