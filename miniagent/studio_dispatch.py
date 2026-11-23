"""Task-addressed controls shared by the Studio assistant's management tools."""
import hashlib
import json

from . import checkpoint, dispatch_notices
from .store import now


def cancel_task(studio, task_id):
    """Pin cancellation to one execution, including a queue entry not yet started."""
    with studio._lock:
        room, record = studio.find_task(task_id)
        run = studio._running.get(room.group.id)
        state = record["status"]
        if run and run.task_id == task_id and state in ("running", "queued"):
            if not run.cancel.is_set():
                studio.cancel(room.group.id)
            return {"task_id":task_id, "status":"stopping"}
        if state in ("queued", "paused"):
            record.update(status="cancelled", finished_at=now(), active=[])
            room.save_task(record)
            room.append("task_finished", task_id=task_id, status="cancelled")
            dispatch_notices.task_outcome(studio, room, record)
            studio._dispatch_changed.notify_all()
            checkpoint.path_for(room,task_id).unlink(missing_ok=True)
            studio._advance(room)
            state = "cancelled"
        return {"task_id":task_id, "status":state}


def submit(studio, origin, group_id, text, *, assignee=None, options=None, inputs=(), previous=None):
    """Deduplicate retries of the same dispatch from the same assistant commission."""
    with studio._lock:
        room = studio.group(group_id)
        if room.group.kind == "assistant":
            raise ValueError("Choose a team conversation or DM.")
        if not isinstance(text, str) or not text.strip() or len(text) > 60000:
            raise ValueError("Supply a brief of 1–60000 characters.")
        if studio.settings.api_key and studio.settings.api_key in text:
            raise ValueError("The brief contains the configured API key.")
        key = [origin, group_id, previous, text, assignee, options, [ref for _, ref in inputs]]
        identity = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()
        sent = room.client_message(identity)
        if sent:
            existing = room.load_task(sent["task_id"])
            if existing["status"] == "failed" and not any(e.get("task_id") == existing["id"] and
                    e["kind"] == "task_started" for e in room.chat()):
                raise ValueError("The previous dispatch could not start: " + existing.get("error","unknown error"))
            return {"route":"received", "group_id":group_id, "task_id":sent["task_id"]}
        studio._require_available(room)
        from .studio import mention
        addressed, body = mention(text, room) if not assignee else (None, text)
        primary = assignee or addressed or room.group.lead
        room.group.member(primary)
        settings, mode = studio._task_settings(room, options or {})
        refs = [studio.uploads.forward(source, room, ref, settings.api_key) for source, ref in inputs]
        result = studio._submit(room, text, body, primary, refs, options or {}, settings, mode,
                                dispatched_by=origin, receipt={"client_id":identity}, redispatched_from=previous)
        return {**result, "group_id":group_id}


def redispatch(studio, origin, task_id, group_id, text=None, assignee=None):
    with studio._lock:
        source, record = studio.find_task(task_id)
        if source.group.kind == "assistant":
            raise ValueError("Select an assignment in a team conversation or DM.")
        events = [e for e in source.chat() if e.get("task_id") == task_id and
                  e["kind"] in ("user_message","queued_message") and not e.get("during_task")]
        if not events:
            raise ValueError("The original task input is unavailable.")
        event = next((e for e in events if e["seq"] == record.get("trigger_seq")), events[0])
        inputs = [(source, ref) for ref in event.get("attachments", [])]
        recipient = assignee or (record["primary"] if group_id == source.group.id else None)
        sent = submit(studio, origin, group_id, record["text"] if text is None else text,
                      assignee=recipient, options=record.get("options"), inputs=inputs, previous=task_id)
        old = cancel_task(studio, task_id)
        return {**sent, "redispatched_from":task_id, "previous_status":old["status"]}


def inspect_room(studio, group_id, offset=0):
    if type(offset) is not int or offset < 0:
        raise ValueError("Invalid offset")
    room = studio.group(group_id)
    info = studio.summary(room)
    tasks = list(reversed(room.tasks()))
    page = tasks[offset:offset+25]
    return {**{k:info[k] for k in ("id","name","team_id","team_name","session_name","lead","mode","pinned",
                                    "archived","workspace_path","running","waiting_for_you")},
            "members":[{"id":m.id,"name":m.name} for m in room.group.members],
            "tasks":[{"task_id":r["id"],"status":r["status"],"primary":r["primary"],
                      "request":r["text"][:300]} for r in page],
            "next_offset":offset+len(page) if offset+len(page)<len(tasks) else None}
