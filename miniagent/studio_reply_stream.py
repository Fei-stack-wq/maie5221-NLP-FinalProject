"""Live public reply text for one running Studio job. Final messages live in the room."""

from collections import deque
from dataclasses import dataclass, field
from threading import Condition


@dataclass
class Draft:
    role: str
    task_id: str
    step: int
    chunks: list[str] = field(default_factory=list)
    pending: str = ""


class ReplyStream:
    def __init__(self, secret):
        self.secret = secret
        self._condition = Condition()
        self._drafts = {}
        self._sequence = 0
        # A lagging listener receives the full current drafts. Evicting old
        # transport frames does not truncate an answer or constrain execution.
        self._frames = deque(maxlen=256)
        self._closed = False

    def _frame(self, event, data):
        self._sequence += 1
        self._frames.append((self._sequence, event, data))
        self._condition.notify_all()

    def _visible_text(self, draft, text, final):
        if not self.secret:
            return text
        pieces = (draft.pending + text).split(self.secret)
        last = pieces[-1]
        draft.pending = ""
        if not final:
            # Do not publish a possible key prefix before the next transport
            # delta disambiguates it. The buffer is at most len(key)-1 chars.
            for size in range(min(len(last), len(self.secret) - 1), 0, -1):
                if last.endswith(self.secret[:size]):
                    draft.pending = last[-size:]
                    pieces[-1] = last[:-size]
                    break
        return "[REDACTED]".join(pieces)

    def publish(self, event):
        kind = event["kind"]
        identity = event["stream_id"]
        with self._condition:
            if self._closed:
                return
            if kind == "discard":
                self._remove(identity)
                return
            if kind not in {"delta", "end"}:
                return
            draft = self._drafts.get(identity)
            if draft is None:
                if kind == "end" or not event.get("text"):
                    return
                draft = self._drafts[identity] = Draft(event["role"], event["task_id"], event["step"])
            text = self._visible_text(draft, event.get("text", "") if kind == "delta" else "", kind == "end")
            if text:
                draft.chunks.append(text)
                self._frame("delta", {"id":identity, "role":draft.role, "task_id":draft.task_id,
                                      "step":draft.step, "text":text})

    def _remove(self, identity):
        if self._drafts.pop(identity, None) is not None:
            self._frame("remove", {"id":identity})

    def room_changed(self, kind, task_id, *, reply_id=None):
        with self._condition:
            if self._closed:
                return
            if kind == "member_report":
                for key in [key for key, draft in self._drafts.items() if draft.task_id == task_id]:
                    del self._drafts[key]
            if reply_id:
                self._drafts.pop(reply_id, None)
            self._frame("room", {"kind":kind, "task_id":task_id, **({"reply_id":reply_id} if reply_id else {})})

    def close(self):
        with self._condition:
            self._closed = True
            self._drafts.clear()
            self._frames.clear()
            self._condition.notify_all()

    def updates(self, cursor):
        """Return a snapshot, ordered new frames, a heartbeat, or the terminal event."""
        with self._condition:
            if cursor is not None:
                self._condition.wait_for(lambda: self._closed or self._sequence != cursor, timeout=10)
            if self._closed:
                return self._sequence, [("done", {})]
            if cursor is None or (self._frames and cursor < self._frames[0][0] - 1):
                replies = [{"id":key, "role":draft.role, "task_id":draft.task_id,
                            "step":draft.step, "text":"".join(draft.chunks)}
                           for key, draft in self._drafts.items() if draft.chunks]
                return self._sequence, [("snapshot", {"replies":replies})]
            frames = [(event, data) for seq, event, data in self._frames if seq > cursor]
            return self._sequence, frames
