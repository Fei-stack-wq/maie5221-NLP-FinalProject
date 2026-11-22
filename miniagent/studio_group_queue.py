"""Admit explicitly requested group work in message order when its room is free."""
from __future__ import annotations

from contextlib import closing
import json
import sqlite3
import uuid

from .studio_group_messages import _database


def record_queued(db, msg, options):
    """Enqueue in the same transaction as the visible group message."""
    db.execute("""CREATE TABLE IF NOT EXISTS work_queue (
        request_id TEXT PRIMARY KEY, options TEXT NOT NULL, prepared TEXT
    )""")
    db.execute("INSERT INTO work_queue(request_id,options) VALUES(?,?)",
               (msg["request_id"], json.dumps(options, ensure_ascii=False)))
    return {**msg, "queue_state": "queued", "text": "已加入本群工作队列。"}


def _next_request(service, root, db):
    from .studio_service import StudioError
    # Close the read transaction before doing service-level work. A concurrent
    # dispatcher may prepare or complete this row in the meantime.
    row = db.execute("""SELECT m.request_id,m.message,q.options,q.prepared
        FROM work_queue q JOIN messages m USING(request_id) ORDER BY m.seq LIMIT 1""").fetchone()
    if not row:
        return None
    identity, raw, options, prepared = row
    if prepared:
        return identity, json.loads(prepared)
    msg = json.loads(raw)
    room = service._conversation(root, with_answers=False)
    if not room or room["id"] != root.name or room["kind"] != "group":
        raise StudioError("找不到排队消息所属的群。", "invalid_group")
    if not room["can_continue"] and room["members"]:
        return None
    options = json.loads(options)
    mode = options.get("mode", "team")
    who = room["lead"] if mode == "team" and room["members"] else msg["role"]
    base = options.pop("conversation", {})
    payload = {**options, "mode": mode, "objective": msg["question"],
               "request_id": str(uuid.uuid5(uuid.NAMESPACE_URL, root.name + "/messages/" + identity)),
               "conversation": {**base, "intent": "edit", "parent_run_id": room["head_run_id"], "recipient": who}}
    # Persist the *exact* payload before admission. After a crash the same request
    # ID and fingerprint find the original run even though the room head moved.
    with db:
        db.execute("UPDATE work_queue SET prepared=? WHERE request_id=? AND prepared IS NULL",
                   (json.dumps(payload, ensure_ascii=False), identity))
        current = db.execute("SELECT prepared FROM work_queue WHERE request_id=?", (identity,)).fetchone()
    return (identity, json.loads(current[0])) if current else None


def _drain_room(service, root):
    from .studio_service import StudioError
    path = _database(root)
    if not path.exists():
        return
    with closing(sqlite3.connect(path, timeout=20)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_queue'").fetchone():
            return
        while not service._closed:
            selected = _next_request(service, root, db)
            if not selected:
                return
            identity, payload = selected
            try:
                run = service.start(payload)
            except StudioError as exc:
                if exc.code in {"busy", "unavailable", "provider_unavailable"}:
                    return
                if exc.code == "stale_head":
                    with db:
                        db.execute("UPDATE work_queue SET prepared=NULL WHERE request_id=? AND prepared=?",
                                   (identity, json.dumps(payload, ensure_ascii=False)))
                    continue
                fields = {"queue_state": "rejected", "text": "这项工作未启动。" + str(exc)}
            else:
                fields = {"queue_state": "started", "linked_run_id": run["id"], "text": "已启动这项工作。"}
            with db:
                removed = db.execute("DELETE FROM work_queue WHERE request_id=? AND prepared=?",
                                     (identity, json.dumps(payload, ensure_ascii=False))).rowcount
                if removed:
                    raw, = db.execute("SELECT message FROM messages WHERE request_id=?", (identity,)).fetchone()
                    updated = {**json.loads(raw), **fields}
                    db.execute("UPDATE messages SET message=? WHERE request_id=?",
                               (json.dumps(updated, ensure_ascii=False), identity))


def drain_group_queue(service, room_id=None):
    """Wake pending work after admission, worker release, or service startup."""
    from .studio_service import StudioError
    roots = [service._directory(room_id)] if room_id else service._directories()
    for root in roots:
        try:
            _drain_room(service, root)
        except (OSError, sqlite3.Error, StudioError):
            # A temporary storage problem in one room must not drop admitted user
            # messages or keep other rooms from taking a newly available slot.
            if room_id:
                raise
