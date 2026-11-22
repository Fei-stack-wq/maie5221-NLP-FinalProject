"""Transient public drafts for one room. The chat log remains the source of committed messages."""
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Condition
from uuid import uuid4


@dataclass
class Draft:
    agent: str
    task_id: str = ""
    request: str = ""
    attachments: tuple = ()
    id: str = field(default_factory=lambda: uuid4().hex)
    parts: list[str] = field(default_factory=list)


class ChatStream:
    def __init__(self, chat_seq):
        self.chat_seq = chat_seq
        self.revision = 0
        self.drafts = {}
        self.changed = Condition()

    def _notify(self):
        self.revision += 1
        self.changed.notify_all()

    @contextmanager
    def room_turn(self, actor, text, refs):
        # turn_lock serializes direct conversation in a room. Workers have separate slots.
        slot = ("", actor)
        with self.changed:
            self.drafts[slot] = Draft(actor, request=text, attachments=tuple(r["name"] for r in refs))
            self._notify()
        try:
            yield lambda phase, part: self.fragment("", actor, phase, part)
        finally:
            with self.changed:
                self.drafts.pop(slot, None)
                self._notify()

    def fragment(self, task_id, actor, phase, text):
        slot = (task_id, actor)
        with self.changed:
            draft = self.drafts.get(slot)
            if phase == "discard":
                if not draft or not draft.parts:
                    return
                # A new identity makes reconnecting and retrying replace the previous attempt.
                if task_id:
                    del self.drafts[slot]
                else:
                    draft.id = uuid4().hex
                    draft.parts.clear()
            elif phase == "delta" and text:
                if draft is None:
                    draft = self.drafts[slot] = Draft(actor, task_id)
                draft.parts.append(text)
            else:
                return
            self._notify()

    def committed(self, event):
        with self.changed:
            self.chat_seq = event["seq"]
            task_id = event.get("task_id")
            if task_id and event["kind"] in ("progress", "report", "final"):
                self.drafts.pop((task_id, event["agent"]), None)
            if task_id and event["kind"] == "task_finished":
                for slot in list(self.drafts):
                    if slot[0] == task_id:
                        del self.drafts[slot]
            self._notify()

    def frames(self):
        revision = -1
        offsets = {}
        while True:
            with self.changed:
                if revision == self.revision:
                    self.changed.wait_for(lambda: revision != self.revision, timeout=10)
                frame = None
                if revision != self.revision:
                    revision = self.revision
                    active = {}
                    patches = []
                    for draft in self.drafts.values():
                        offset = offsets.get(draft.id, 0)
                        active[draft.id] = len(draft.parts)
                        if draft.id not in offsets or offset < len(draft.parts):
                            patch = {"id":draft.id, "text":"".join(draft.parts[offset:])}
                            if draft.id not in offsets:
                                patch.update(agent=draft.agent, task_id=draft.task_id,
                                             request=draft.request, attachments=draft.attachments)
                            patches.append(patch)
                    frame = {"after":self.chat_seq, "drafts":patches,
                             "removed":list(offsets.keys()-active.keys())}
                    offsets = active
            # No lock is held during network I/O. A slow reader sees coalesced increments.
            yield frame
