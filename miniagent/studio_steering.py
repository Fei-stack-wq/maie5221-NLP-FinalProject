"""One durable public conversation input, with a separate cursor for each executing role."""
from __future__ import annotations

import json
import uuid

from .studio_group_status import previous_status
from .studio_uploads import UploadError, resolve_uploads, validate_upload_ids


def steering_rows(events):
    acknowledged = {identity: event["role"] for event in events if event["kind"] == "steer_seen"
                    for identity in event["request_ids"]}
    return [{**row, "seen_by": acknowledged.get(row["request_id"])}
            for row in events if row["kind"] == "user_steer"]


class RunSteering:
    def __init__(self, lock, record):
        self.lock = lock
        self.record = record
        self.rows = []
        self.seen = set()
        self.sealed = False
        self.active_cursor = None

    def subscribe(self, role, task_id, *, owner=False):
        return SteeringCursor(self, role, task_id, owner)

    def attachments(self):
        with self.lock:
            return [ref for row in self.rows for ref in row.get("message_attachments", [])]


class SteeringCursor:
    def __init__(self, inbox, role, task_id, owner):
        self.inbox, self.role, self.task_id, self.owner = inbox, role, task_id, owner
        self.position = 0
        self.acknowledged = 0

    def pending(self):
        with self.inbox.lock:
            return self.position < len(self.inbox.rows)

    def take(self, *, finishing=False):
        with self.inbox.lock:
            self.inbox.active_cursor = self
            rows = self.inbox.rows[self.position:]
            self.position += len(rows)
            if finishing and self.owner and not rows:
                self.inbox.sealed = True
            return [row["text"] + ("\n此次调整引用的群内讨论（历史资料）：" + json.dumps(row["preceding_dialogue"], ensure_ascii=False)
                                   if row.get("preceding_dialogue") else "") + ("\n随本次补充发送的附件：" + json.dumps(row["message_attachments"], ensure_ascii=False)
                                   if row.get("message_attachments") else "") for row in rows]

    def acknowledge(self):
        with self.inbox.lock:
            ids = [row["request_id"] for row in self.inbox.rows[self.acknowledged:self.position]
                   if row["request_id"] not in self.inbox.seen]
            if ids:
                self.inbox.record(self.role, "steer_seen", "", task_id=self.task_id, request_ids=ids)
                self.inbox.seen.update(ids)
            self.acknowledged = self.position

    def close(self):
        if self.owner:
            with self.inbox.lock:
                self.inbox.sealed = True


class StudioSteering:
    def accepts_steering(self, run_id):
        with self._lock:
            inbox = self._steering.get(run_id)
            return bool(inbox and not inbox.sealed and not self._closed and not self._cancellations[run_id].is_set())

    def steer(self, run_id, payload, *, dialogue=()):
        from .studio_service import StudioError

        if not isinstance(payload, dict) or set(payload) - {"request_id", "message", "attachments"}:
            raise StudioError("运行中消息需要请求 ID、正文或附件。")
        identity = payload.get("request_id")
        try:
            if not isinstance(identity, str) or str(uuid.UUID(identity)) != identity:
                raise ValueError()
            ids = validate_upload_ids(payload.get("attachments", []))
        except UploadError as exc:
            raise StudioError(str(exc), exc.code, exc.status) from None
        except ValueError:
            raise StudioError("request_id 须为 UUID。") from None
        text = payload.get("message", "")
        if not isinstance(text, str) or len(text) > 10000 or not (text.strip() or ids):
            raise StudioError("请输入不超过 10000 字符的消息或选择附件。")
        text = self._redact(text.strip())
        with self._answers:
            path = self._directory(run_id)
            if self._meta(path).get("has_status_replies"):
                replay = previous_status(self, path, identity, text, attachments=bool(ids))
                if replay:
                    return replay
            inbox = self._steering.get(run_id)
            rows = inbox.rows if inbox else [row for row in self.room_events(run_id) if row["kind"] == "user_steer"]
            for row in rows:
                if row["request_id"] == identity:
                    if row["text"] != text or [item["id"] for item in row.get("message_attachments", [])] != ids:
                        raise StudioError("同一请求 ID 不能用来发送不同消息。", "steer_conflict", 409)
                    return row
            if inbox is None:
                meta = self._meta(path)
                if run_id in self._active:
                    raise StudioError("此运行没有工作室聊天入口。", "not_chat", 409)
                if meta.get("state") in {"queued", "running"} and self._assistant_elsewhere(path):
                    raise StudioError("请在正在执行此任务的 Studio 服务中发送消息。", "different_executor", 409)
                raise StudioError("本轮已经结束，可以作为下一条消息发送。", "run_finished", 409)
            if inbox.sealed or self._closed or self._cancellations[run_id].is_set():
                raise StudioError("本轮正在保存结果，可以在结束后作为下一条消息发送。", "run_closing", 409)
            try:
                refs = resolve_uploads(self.runs_dir, ids)
            except UploadError as exc:
                raise StudioError(str(exc), exc.code, exc.status) from None
            if not inbox.rows:
                # Publish the inexpensive index before the durable entry. An
                # interrupted append may leave an empty indexed journal, never a hidden message.
                self._update_meta(run_id, has_steering=True)
            row = inbox.record("user", "user_steer", text, request_id=identity, message_attachments=refs,
                               **({"preceding_dialogue": dialogue} if dialogue else {}))
            inbox.rows.append(row)
            for (rid, _), waiting in self._questions.items():
                if rid == run_id and waiting["answer"] is None:
                    waiting["steered"] = True
            self._answers.notify_all()
            return row
