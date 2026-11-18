"""Public collaboration journal, live replies and assessor exports for Studio."""
from __future__ import annotations

import hashlib
import io
import json
import os
import time
import uuid
import zipfile
from datetime import datetime, timezone


class RoomDiscussion:
    def room_events(self, run_id):
        from .studio_service import StudioError, _read_file
        with self._lock:
            data, truncated = _read_file(self._directory(run_id), "public-room.jsonl", 16 * 1024**2, missing=True)
            if truncated:
                raise StudioError("本轮公开对话超过导出上限。", "room_too_large", 413)
            return [json.loads(line) for line in data.splitlines() if line]

    def _record_room_event(self, run_id, role, kind, text, **fields):
        from .studio_service import _now
        row = self._redact({**fields, "role": role, "kind": kind, "text": text, "at": _now()})
        data = (json.dumps(row, ensure_ascii=False) + "\n").encode()
        with self._lock:
            journal = self._directory(run_id) / "public-room.jsonl"
            fd = os.open(journal, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "ab") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        return row

    def _human_handler(self, run_id, role, *, deadline=None, request_kind="question"):
        def ask(question):
            if not question.strip():
                return None
            with self._answers:
                path = self._directory(run_id)
                meta = self._meta(path)
                limit = meta.get("request", {}).get("limits", {}).get("max_seconds", self.settings.limits.max_seconds)
                start = datetime.fromisoformat(meta["created_at"].replace("Z", "+00:00"))
                remaining = limit - (datetime.now(timezone.utc) - start).total_seconds()
                room_deadline = time.monotonic() + max(0, remaining)
                expires = min(room_deadline, deadline) if deadline is not None else room_deadline
                identity = uuid.uuid4().hex
                key = (run_id, identity)
                cancellation = self._cancellations.get(run_id)
                if self._closed or cancellation is None or cancellation.is_set():
                    return None
                started = time.monotonic()
                action_digest = hashlib.sha256(question.encode()).hexdigest() if request_kind == "approval" else None
                self._record_room_event(run_id, role, "human_question", question, request_id=identity,
                                        request_kind=request_kind, action_digest=action_digest)
                self._questions[key] = {"request_id": identity, "role": role, "text": self._redact(question),
                                       "answer": None, "deadline": expires, "started": started,
                                       "request_kind": request_kind, "action_digest": action_digest}
                try:
                    while self._questions[key]["answer"] is None:
                        if self._closed or cancellation.is_set() or time.monotonic() >= expires:
                            if time.monotonic() >= room_deadline:
                                cancellation.set()
                            self._record_room_event(run_id, role, "question_closed", "等待结束，本次没有收到答复。",
                                                    request_id=identity, request_kind=request_kind,
                                                    wait_seconds=round(time.monotonic() - started, 6))
                            return None
                        self._answers.wait(timeout=max(0.01, expires - time.monotonic()))
                    return self._questions[key]["answer"]
                finally:
                    del self._questions[key]
                    self._answers.notify_all()
        return ask

    def _approval_handler(self, run_id, role, *, deadline=None):
        ask = self._human_handler(run_id, role, deadline=deadline, request_kind="approval")
        def approve(action):
            answer = ask(action + "\n请明确回答批准或拒绝。")
            return isinstance(answer, str) and answer.strip().lower() in {"y", "yes", "allow", "批准", "同意"}
        return approve

    def reply_human(self, run_id, request_id, answer):
        from .studio_service import StudioError
        if type(answer) is not str or not 0 < len(answer.strip()) <= 8000:
            raise StudioError("答复需要 1 至 8000 个字符。")
        answer = self._redact(answer.strip())
        with self._answers:
            for row in self.room_events(run_id):
                if row["kind"] == "human_answer" and row["request_id"] == request_id:
                    if row["text"] == answer:
                        return row
                    raise StudioError("这条问题已经回答。", "answer_conflict", 409)
            pending = self._questions.get((run_id, request_id))
            if (pending is None or self._closed or self._cancellations[run_id].is_set()
                    or time.monotonic() >= pending["deadline"]):
                raise StudioError("本次执行没有在等待这条答复。", "not_waiting", 409)
            decision = None
            if pending.get("request_kind") == "approval":
                allowed = {"y", "yes", "allow", "批准", "同意"}
                denied = {"n", "no", "deny", "拒绝", "不同意"}
                if answer.lower() not in allowed | denied:
                    raise StudioError("审批需要明确回答批准或拒绝。")
                decision = "allow" if answer.lower() in allowed else "deny"
            row = self._record_room_event(run_id, "user", "human_answer", answer, request_id=request_id,
                                          recipient=pending["role"], request_kind=pending.get("request_kind", "question"),
                                          decision=decision, action_digest=pending.get("action_digest"),
                                          wait_seconds=round(time.monotonic() - pending["started"], 6))
            pending["answer"] = answer
            self._answers.notify_all()
            return row

    @staticmethod
    def human_timing(events):
        questions = {e["request_id"] for e in events if e["kind"] == "human_question"}
        completed = {e["request_id"]: e for e in events if e["kind"] in {"human_answer", "question_closed"}}
        measured = [e["wait_seconds"] for e in completed.values() if "wait_seconds" in e]
        return {"request_count": len(questions), "resolved_count": len(questions & completed.keys()),
                "waiting_seconds": round(sum(measured), 6),
                "measurement_complete": all(i in completed and "wait_seconds" in completed[i] for i in questions)}

    def review_delivery(self, run_id, verdict, note):
        from .studio_service import StudioError, _TERMINAL
        if verdict not in ("passed", "failed") or type(note) is not str or not 0 < len(note.strip()) <= 8000:
            raise StudioError("请选择评审结论并写下检验过程。")
        with self._lock:
            summary = self._summary(self._directory(run_id))
            if summary["status"] not in _TERMINAL:
                raise StudioError("请在本轮结束后评审。", "run_active", 409)
            prior = next((row for row in reversed(self.room_events(run_id)) if row["kind"] == "human_review"), None)
            if prior and prior["verdict"] == verdict and prior["text"] == self._redact(note.strip()):
                return prior
            return self._record_room_event(run_id, "user", "human_review", note.strip(), verdict=verdict)

    def room_bundle(self, run_id):
        from .studio_service import StudioError, _read_file, _TERMINAL
        cap = 64 * 1024**2
        stream, size = io.BytesIO(), 0
        def add(archive, path, data):
            nonlocal size
            size += len(data)
            if size > cap:
                raise StudioError("会话材料超过 64 MiB，请按轮下载。", "room_too_large", 413)
            archive.writestr(path, data)
        def encode(value):
            return json.dumps(self._redact(value), indent=2, ensure_ascii=False).encode()
        with self._lock:
            room = self._conversation(self._directory(run_id), with_answers=False)
            if not room:
                raise StudioError("该运行没有会话。", "not_found", 404)
            transcript_size = 0
            transcript, manifest = [], []
            with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as packet:
                for i, turn in enumerate(room["turns"], 1):
                    identity = turn["run_id"]
                    if turn["status"] not in _TERMINAL:
                        raise StudioError("请在各轮执行结束后导出会话。", "run_active", 409)
                    root = self._directory(identity)
                    try:
                        answer, truncated = _read_file(root, "conversation-answer.txt", max(0, cap - size - transcript_size))
                        available = True
                    except StudioError as exc:
                        if exc.code != "not_found":
                            raise
                        answer, truncated, available = b"", False, False
                    if truncated:
                        raise StudioError("会话答复超过导出上限。", "room_too_large", 413)
                    public = {**turn, "events": self.room_events(identity), "answer": answer.decode(),
                              "answer_available": available, "answer_truncated": False}
                    transcript_size += len(encode(public))
                    if size + transcript_size > cap:
                        raise StudioError("会话材料超过 64 MiB，请按轮下载。", "room_too_large", 413)
                    transcript.append(public)
                    tasks, checks = self._records(root)
                    prefix = f"turn-{i:03d}"
                    request = self._meta(root).get("request", {})
                    audit_files = []
                    # Preserve complete bounded tool receipts, not only the UI's
                    # last MAX_EVENTS slice. Export never includes model reasoning.
                    audit = {"request": request, "thread": self._meta(root).get("thread"),
                             "timing": self.human_timing(public["events"]),
                             "wall_clock": {k: self._summary(root).get(k) for k in ("created_at", "finished_at", "duration_ms")},
                             "runtime": self._meta(root).get("runtime_identity"),
                             "runtime_identity_available": "runtime_identity" in self._meta(root)}
                    items = [("execution.json", encode(audit))]
                    for filename in ("trace.jsonl", "board.json", "plan.json", "bus.jsonl", "role-memory.json"):
                        raw, truncated = _read_file(root, filename, cap - size - transcript_size, missing=True)
                        if truncated:
                            raise StudioError("运行记录超过导出上限。", "room_too_large", 413)
                        if raw:
                            # Redact configured credentials recursively through
                            # the existing service policy, retaining every event.
                            parsed = ([json.loads(line) for line in raw.splitlines() if line]
                                      if filename.endswith("jsonl") else json.loads(raw))
                            content = (("\n".join(json.dumps(self._redact(row), ensure_ascii=False) for row in parsed) + "\n").encode()
                                       if filename.endswith("jsonl") else encode(parsed))
                            items.append((filename, content))
                    for filename, content in items:
                        location = f"{prefix}/audit/{filename}"
                        add(packet, location, content)
                        audit_files.append({"path": location, "sha256": hashlib.sha256(content).hexdigest()})
                    add(packet, f"{prefix}/assessment.json", encode({"tasks": tasks, "checks": checks,
                        "acceptance_files": request.get("acceptance_files", []),
                        "criteria": request.get("acceptance_checks", []), "child_checks": request.get("child_checks", {})}))
                    files = []
                    if self._artifacts(root, tasks):
                        work = self.artifact_bundle(identity)
                        with zipfile.ZipFile(io.BytesIO(work)) as payload:
                            for file in payload.infolist():
                                if size + file.file_size + transcript_size > cap:
                                    raise StudioError("会话材料超过 64 MiB，请按轮下载。", "room_too_large", 413)
                                content = payload.read(file)
                                name = f"{prefix}/artifacts/{file.filename}"
                                add(packet, name, content)
                                files.append({"path": name, "sha256": hashlib.sha256(content).hexdigest()})
                    manifest.append({"run_id": identity, "turn": i, "status": turn["status"], "files": files,
                                     "audit_files": audit_files, "human_timing": self.human_timing(public["events"])})
                add(packet, "conversation.json", encode({**room, "turns": transcript}))
                add(packet, "manifest.json", encode(manifest))
            return stream.getvalue()
