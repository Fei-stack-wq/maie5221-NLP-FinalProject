"""Durable group conversation. A message may link to a task, but never owns one."""
from __future__ import annotations

from contextlib import closing
from hashlib import sha256
import json
import os
import sqlite3
import uuid

from .studio_conversations import addressed_members
from .studio_uploads import UploadError, resolve_uploads, validate_upload_ids


def _database(root):
    from .studio_service import StudioError
    path = root / "room-messages.sqlite3"
    for suffix in ("", "-journal", "-shm", "-wal"):
        if os.path.islink(str(path) + suffix):
            raise StudioError("群消息存储路径无效。", "storage_error", 409)
    return path


def read_messages(service, room_id, *, latest_only=False):
    path = _database(service._directory(room_id))
    if not path.exists():
        return []
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'").fetchone():
            return []
        order = "DESC LIMIT 1" if latest_only else "ASC"
        rows = db.execute("SELECT seq, message FROM messages WHERE published=1 ORDER BY seq " + order)
        return [{**json.loads(data), "seq": seq} for seq, data in rows]


def recent_dialogue(messages):
    """Carry dialogue into explicit work without turning old remarks into new orders."""
    return [{"user": row["question"][:1200], "role": row["role"], "reply": row["text"][:1600]}
            for row in messages if row["disposition"] == "reply"][-8:]


def post_message(service, room_id, payload):
    from .studio_group_queue import record_queued, drain_group_queue
    from .studio_room_reply import decide_room_message
    from .studio_service import StudioError, _now

    if not isinstance(payload, dict) or payload.keys() - {"request_id", "text", "parent_run_id", "recipient", "intent", "options"}:
        raise StudioError("群消息参数无效。")
    identity = payload.get("request_id")
    try:
        if str(uuid.UUID(str(identity))) != identity:
            raise ValueError()
    except (TypeError, ValueError):
        raise StudioError("request_id 须为 UUID。") from None
    intent = payload.get("intent", "auto")
    options = payload.get("options", {})
    if intent not in ("auto", "question", "work", "steer") or not isinstance(options, dict) or options.keys() - {
            "mode", "thinking", "interaction_mode", "limits", "acceptance_files", "acceptance_checks",
            "child_checks", "required_source_review_paths", "attachments", "conversation"}:
        raise StudioError("群消息意图或工作选项无效。")
    thinking = options.get("thinking", service.settings.thinking)
    if thinking not in ("enabled", "disabled"):
        raise StudioError("thinking 必须为 disabled 或 enabled。")
    if options.get("mode", "team") not in ("single", "team"):
        raise StudioError("群工作需要成员或团队执行模式。")
    try:
        ids = validate_upload_ids(options.get("attachments", []))
        attachments = resolve_uploads(service.runs_dir, ids)
    except UploadError as exc:
        raise StudioError(str(exc), exc.code, exc.status) from None
    text = payload.get("text", "")
    if not isinstance(text, str) or len(text) > 10000 or not (text.strip() or ids):
        raise StudioError("请输入消息或选择附件。")
    text = service._redact(text.strip())
    base = options.get("conversation", {})
    if not isinstance(base, dict) or base.keys() - {"inherit_files", "recover_failed", "recover_task_id"}:
        raise StudioError("群任务的文件选项无效。")
    fingerprint = sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    service._membership_root(room_id)
    root = service._directory(room_id)
    path = _database(root)
    # Only competing sends to this room serialize. Workers, readers and other
    # rooms never wait behind a global lock during a provider request.
    with closing(sqlite3.connect(path, timeout=20)) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS messages(
            seq INTEGER PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL,
            message TEXT NOT NULL, published INTEGER NOT NULL)""")
        db.execute("BEGIN IMMEDIATE")
        saved = db.execute("SELECT seq,fingerprint,message,published FROM messages WHERE request_id=?", (identity,)).fetchone()
        if saved:
            seq, original, data, published = saved
            if original != fingerprint:
                raise StudioError("不能以同一个请求编号发送不同的群消息。", "conflict", 409)
            msg = json.loads(data)
            if published:
                return _response(service, msg, seq)
        else:
            room = service._conversation(root, with_answers=False)
            head = room["head_run_id"]
            if not head or payload.get("parent_run_id") != head:
                raise StudioError("群内任务已更新，请刷新群聊。", "stale_head", 409)
            if not room["members"]:
                raise StudioError("请先邀请一名群成员。", "empty_group", 409)
            try:
                addressed = addressed_members(text, room)
            except ValueError as exc:
                raise StudioError(str(exc), "invalid_addressee") from None
            speaker = payload.get("recipient") or room["lead"]
            if speaker not in room["members"]:
                raise StudioError("该角色不在本群中。", "non_member", 400)
            if len(addressed) == 1:
                speaker = addressed[0]
            active_states = {"queued", "running", "waiting_user"}
            head_path = service._directory(head)
            active = service._summary(head_path)["status"] in active_states
            if intent in ("work", "steer"):
                decision = {"action": "steer" if intent == "steer" else "work", "text": ""}
            else:
                decision = decide_room_message(service, room, text, active=active, recipient=speaker, attachments=attachments,
                                               reply_only=intent == "question", thinking=thinking)
            action = decision["action"]
            answer = decision["text"]
            # A worker can publish while the room answers a question. Order the
            # reply and apply changes against the state when the answer arrives.
            if active:
                active = service._summary(head_path)["status"] in active_states
            if intent == "question" and action != "reply":
                action, answer = "reply", "这条消息按只读问答处理。请说明你想了解哪个方面。"
            if action == "steer" and not active:
                action, answer = "reply", "当前任务已经结束。你可以指出要基于哪份交付继续修改。"
            msg = {"kind": "group_message", "request_id": identity, "parent_run_id": head, "question": text,
                   "role": speaker, "disposition": action, "text": answer, "at": _now(), "after_turn": not active}
            if attachments:
                msg["message_attachments"] = attachments
            if decision.get("model_receipt"):
                msg["model_receipt"] = decision["model_receipt"]
            if action == "reply" and decision.get("cited_artifacts"):
                msg["cited_artifacts"] = decision["cited_artifacts"]
            if action == "work":
                # Prepare the queue entry and visible message in the same commit.
                msg = record_queued(db, msg, {**options, "thinking": thinking})
            seq = db.execute("INSERT INTO messages(request_id,fingerprint,message,published) VALUES(?,?,?,?)",
                             (identity, fingerprint, json.dumps(msg, ensure_ascii=False), int(action != "steer"))).lastrowid
            # Persist the chosen action before it can have side effects. A retry
            # replays that decision and uses the same request ID with the task store.
            db.commit()
        if msg["disposition"] == "work":
            db.commit()
            drain_group_queue(service, room_id)
            data = db.execute("SELECT message FROM messages WHERE request_id=?", (identity,)).fetchone()[0]
            return _response(service, json.loads(data), seq)
        if msg["disposition"] == "reply":
            return _response(service, msg, seq)
        head = msg["parent_run_id"]
        action_id = str(uuid.uuid5(uuid.NAMESPACE_URL, room_id + "/messages/" + identity))
        try:
            service.steer(head, {"request_id": action_id, "message": text, "attachments": ids},
                          dialogue=recent_dialogue(read_messages(service, room_id)))
        except StudioError as exc:
            if exc.code not in {"run_closing", "run_finished", "different_executor", "not_chat"}:
                raise
            msg.update(disposition="reply", text="这条要求未应用。" + str(exc), after_turn=True)
        else:
            msg["linked_run_id"] = head
        db.execute("UPDATE messages SET published=1,message=? WHERE request_id=?", (json.dumps(msg, ensure_ascii=False), identity))
        db.commit()
        return _response(service, msg, seq)


def _response(service, message, seq):
    response = {"message": {**message, "seq": seq}}
    if message["disposition"] == "work" and message.get("linked_run_id"):
        response["run"] = service._redact(service._summary(service._directory(message["linked_run_id"])))
    return response
