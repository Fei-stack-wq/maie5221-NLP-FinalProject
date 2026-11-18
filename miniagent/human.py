"""Synchronous CLI questions/approvals with explicit EOF and timeout semantics."""
from __future__ import annotations

import queue
import sys
import threading
import time
import uuid


def bounded_call(callback, prompt: str, seconds: float | None, *, task_id: str = ""):
    if hasattr(callback, "with_timeout"):
        return callback.with_timeout(prompt, seconds, task_id=task_id)
    if seconds is None:
        return callback(prompt)
    if seconds <= 0:
        raise TimeoutError("human response deadline exhausted")
    replies = queue.Queue(maxsize=1)
    def read():
        try:
            replies.put((True, callback(prompt)))
        except Exception as exc:
            replies.put((False, exc))
    threading.Thread(target=read, daemon=True).start()
    try:
        ok, reply = replies.get(timeout=seconds)
    except queue.Empty:
        raise TimeoutError("human response deadline exhausted") from None
    if not ok:
        raise reply
    return reply


class _Handler:
    def __init__(self, channel, kind):
        self.channel, self.kind = channel, kind

    def __call__(self, prompt):
        return self.with_timeout(prompt, None)

    def with_timeout(self, prompt, seconds, *, task_id=""):
        return self.channel._request(self.kind, prompt, seconds, task_id)


class TerminalHuman:
    def __init__(self, *, stdin=None, stdout=None, auto_approve=False, emit=None):
        self.stdin = stdin if stdin is not None else sys.stdin
        self.stdout = stdout if stdout is not None else sys.stderr
        self.auto_approve, self.emit = auto_approve, emit
        self._lock = threading.Lock()
        self._unavailable = False
        self.ask = _Handler(self, "question")
        self.approve = _Handler(self, "approval")

    def _request(self, kind, prompt, seconds, task_id):
        deadline = None if seconds is None else time.monotonic() + max(0, seconds)
        acquired = self._lock.acquire(timeout=max(0, seconds)) if seconds is not None else self._lock.acquire()
        if not acquired:
            return False if kind == "approval" else None
        try:
            request_id = uuid.uuid4().hex
            source = "policy" if kind == "approval" and self.auto_approve else "stdin"
            if self.emit:
                self.emit("human_request", request_id=request_id, request_kind=kind, prompt=prompt, source=source, owner_task_id=task_id)
            answer, status = None, "unavailable"
            if source == "policy":
                answer, status = True, "approved"
            elif not self._unavailable:
                suffix = " [y/N] " if kind == "approval" else "\n> "
                self.stdout.write(f"\n[{'批准操作' if kind == 'approval' else '人工提问'}] {prompt}{suffix}")
                self.stdout.flush()
                remaining = None if deadline is None else max(0, deadline - time.monotonic())
                try:
                    raw = bounded_call(lambda _: self.stdin.readline(), "", remaining)
                    value = raw.strip()
                    if not raw:
                        self._unavailable = True
                        status = "eof"
                    elif kind == "approval":
                        answer = value.lower() in {"y", "yes", "是", "同意"}
                        status = "approved" if answer else "denied"
                    elif value:
                        answer, status = value, "answered"
                    else:
                        status = "empty"
                except (EOFError, OSError, TimeoutError):
                    self._unavailable = True
                    status = "unavailable_or_timeout"
            if self.emit:
                self.emit("human_reply", request_id=request_id, request_kind=kind,
                          source=source, status=status, answer=answer, owner_task_id=task_id)
            return bool(answer) if kind == "approval" else answer
        finally:
            self._lock.release()
