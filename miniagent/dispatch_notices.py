"""Project committed group/task records into their originating assistant conversation."""
import logging

from .evaluation import task_changes


def _deliver(studio, target, origin, kind, identity, **fields):
    if not origin:
        return
    try:
        source = studio.store.get(origin["group_id"])
        if source.group.kind == "assistant" and (source.root/"tasks"/origin["task_id"]/"task.json").is_file():
            source.append_notice(kind, f"{kind}:{identity}", task_id=origin["task_id"],
                                 target_group_id=target.group.id, group_name=target.group.name, **fields)
    except KeyError:
        return  # The source no longer exists.
    except OSError:
        # The committed target record is the replay source. Notification I/O cannot undo a dispatch.
        logging.getLogger(__name__).exception("Could not deliver %s for %s", kind, identity)


def group_created(studio, target):
    _deliver(studio, target, target.group.created_by, "assistant_group_created", target.group.id,
             room_kind=target.group.kind)


def task_receipt(studio, target, record, *, legacy_origin=None):
    tid = record["id"]
    _deliver(studio, target, record.get("dispatched_by") or legacy_origin, "dispatch_sent", tid,
             target_task_id=tid, queued="queued_message" in record,
             **({"fast": True} if (record.get("options") or {}).get("mode") == "fast" else {}))


def task_outcome(studio, target, record, *, legacy_origin=None):
    origin = record.get("dispatched_by") or legacy_origin
    if not origin or record["status"] in ("queued", "running", "paused"):
        return
    tid = record["id"]
    published = {c["path"]:c for c in task_changes(target, tid)}
    evaluation = record.get("evaluation") or {}
    _deliver(studio, target, origin, "dispatch_finished", tid, target_task_id=tid,
             status=record["status"], text=record.get("final_answer", ""),
             error=record.get("error", ""), warning=record.get("delivery_warning", ""),
             evaluation={k:evaluation[k] for k in ("passed","total") if k in evaluation},
             artifacts=list(published.values()))
