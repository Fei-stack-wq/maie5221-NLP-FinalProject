"""Directory metadata and the unified assistant's own cross-room dispatch receipts."""
import json
from functools import lru_cache

from .room_turn import read_excerpt, task_evidence


def directory(groups):
    return [{"id":g.id, "name":g.name, "kind":g.kind, "lead":g.lead,
             "members":[m.id for m in g.members], "archived":g.archived}
            for g in groups if g.kind != "assistant"]


@lru_cache(maxsize=128)
def _legacy_receipts(path, size, mtime_ns):
    # Pre-routing assistants only kept their dispatch receipts in their own tool trace.
    ids = set()
    with path.open("rb") as stream:
        for line in stream:
            if b'"start_task"' not in line or not line.endswith(b"\n"):
                continue
            try:
                event = json.loads(line)
                if isinstance(event, dict) and event.get("type") == "tool_result" and event.get("tool") == "start_task" and event.get("ok"):
                    receipt = json.loads(event["preview"])
                    if isinstance(receipt, dict) and isinstance(receipt.get("task_id"), str):
                        ids.add(receipt["task_id"])
            except (ValueError, KeyError, TypeError):
                continue
    return frozenset(ids)


def legacy_dispatch_origins(source):
    legacy = {}
    for parent in source.tasks():
        if "room_kind" not in parent:
            path = source.root / "tasks" / parent["id"] / "trace.jsonl"
            try:
                stat = path.stat()
                for child in _legacy_receipts(path, stat.st_size, stat.st_mtime_ns):
                    legacy[child] = {"group_id":source.group.id, "task_id":parent["id"]}
            except FileNotFoundError:
                pass
    return legacy


def workspace_evidence(studio, source):
    groups = studio.store.groups()
    legacy = legacy_dispatch_origins(source)
    receipts = []
    for group in groups:
        if group.kind == "assistant":
            continue
        try:
            target = studio.store.get(group.id)
            tasks = target.tasks()
        except (KeyError, FileNotFoundError):
            continue  # The room was moved to trash after the directory listing.
        for task in tasks:
            origin = task.get("dispatched_by") or {}
            if origin.get("group_id") == source.group.id or (not origin and task["id"] in legacy):
                receipts.append((target, task))
    receipts.sort(key=lambda pair:pair[1].get("created_at", 0))
    live = [pair for pair in receipts if pair[1]["status"] in ("queued", "running")]
    finished = [pair for pair in receipts if pair[1]["status"] not in ("queued", "running")]
    dispatches = []
    histories = {}
    for target, task in finished[-4:] + live:
        if target.group.id not in histories:
            histories[target.group.id] = target.project.history(0)
        summary = task_evidence(task, histories[target.group.id])
        dispatches.append({**summary, "group_id":target.group.id, "group_name":target.group.name})
    roles = [{"id":r["id"], "name":r["name"], "duty":r["prompt"][:350]} for r in studio.roles.list()]
    # Keep older deliveries discoverable without injecting every earlier report into every turn.
    earlier = [{"id":t["id"], "group_id":target.group.id, "group_name":target.group.name,
                "request":t.get("text", "")[:240], "status":t["status"]} for target,t in finished[:-4]]
    return {"directory":{"roles":roles, "rooms":directory(groups)}, "dispatches":dispatches, "earlier_dispatches":earlier}


def read_dispatch(studio, receipt, args):
    try:
        target = studio.store.get(receipt["group_id"])
        if set(args) == {"task_id"}:
            path = target.root / "tasks" / receipt["id"] / "task.json"
            task = json.loads(path.read_bytes())
            return json.dumps(task_evidence(task,target.project.history(0)),ensure_ascii=False)
        return read_excerpt(target, studio.uploads, {"tasks":[receipt],"attachments":[]}, args)
    except (FileNotFoundError, KeyError):
        return "The original dispatch is no longer available."


def progress(running):
    with running.lock:
        last = next((e for e in reversed(running.activity)
                     if e["type"] in ("tool_call", "tool_result", "step", "say")), {})
        summary = {key:last[key] for key in ("type","agent","tool","ok","step") if key in last}
        if last.get("type") == "say":
            summary["text"] = last.get("text", "")[:1200]
        return summary
