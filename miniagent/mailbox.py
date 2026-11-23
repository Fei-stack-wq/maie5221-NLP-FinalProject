"""Task-local requester questions, addressed mail and delivery receipts."""
from __future__ import annotations

import copy
import threading
import time
import uuid
from typing import Callable

from .store import GroupStore


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------------------
# Requester channel: questions and live messages
# ---------------------------------------------------------------------------
class HumanChannel:
    """Questions agents ask the requester, plus messages the requester sends mid-task."""

    def __init__(self, store: GroupStore, task_id: str, should_cancel: Callable[[], bool]) -> None:
        self.store = store
        self.task_id = task_id
        self.should_cancel = should_cancel
        self._cond = threading.Condition()
        self._pending: dict | None = None
        self._answer: str | None = None
        self._messages: list[tuple[int, str]] = []
        self._finished = False
        self._cursors: dict[str, int] = {}
        self._notes: dict[str, list[str]] = {}
        self.questions = 0
        self.control = None
        self.enabled = self.interrupts = False
        self.primary = store.group.lead
        self.names = {m.id: m.name for m in store.group.members}
        self.active = set()
        self.mail = []
        self.signals = {}
        self.last_interrupt = {}
        self.interrupt_mail = {}
        self.interrupted = set()
        self.on_event = self.on_user_mail = None

    def snapshot(self):
        with self._cond:
            return {"pending":self._pending, "answer":self._answer, "messages":list(self._messages),
                    "cursors":dict(self._cursors), "questions":self.questions,
                    "notes":{agent: list(items) for agent, items in self._notes.items()},
                    "mail":copy.deepcopy(self.mail), "last_interrupt":dict(self.last_interrupt),
                    "interrupt_mail":dict(self.interrupt_mail), "interrupted":list(self.interrupted)}

    def restore(self, saved):
        self._pending, self._answer = saved["pending"], saved["answer"]
        self._messages, self._cursors, self.questions = saved["messages"], saved["cursors"], saved["questions"]
        self._notes = {agent: list(items) for agent, items in (saved.get("notes") or {}).items()}
        self.mail = copy.deepcopy(saved.get("mail", []))
        self.last_interrupt = dict(saved.get("last_interrupt", {}))
        self.interrupt_mail = dict(saved.get("interrupt_mail", {}))
        self.interrupted = set(saved.get("interrupted", []))
        for agent in self.interrupt_mail:
            self.signal(agent).set()

    def signal(self, agent):
        with self._cond:
            return self.signals.setdefault(agent, threading.Event())

    def mark_interrupted(self, agent):
        with self._cond:
            if agent in self.interrupt_mail:
                self.interrupted.add(agent)

    def activate(self, agent):
        with self._cond:
            self.active.add(agent)

    def deactivate(self, agent):
        with self._cond:
            self.active.discard(agent)

    def recent(self):
        with self._cond:
            return copy.deepcopy(self.mail[-60:])

    def post(self, sender, to, kind, text, *, urgent=False, queue_to=None, **fields):
        with self._cond:
            if not isinstance(to, (list, tuple)) or any(not isinstance(a,str) for a in to):
                raise ValueError("Expected a list of mailbox recipients")
            targets = list(dict.fromkeys(to or ["*"]))
            if targets != ["*"] and any(agent not in self.names for agent in targets):
                raise ValueError("Unknown task mailbox recipient")
            if urgent and sender not in {"user", self.primary}:
                raise ValueError("Only the requester or task lead can interrupt")
            recipients = sorted(self.active | {self.primary}) if targets == ["*"] else targets
            if queue_to is not None:
                if any(not isinstance(agent, str) or agent not in self.names for agent in queue_to):
                    raise ValueError("Unknown delivery recipient")
                recipients = list(dict.fromkeys(queue_to))
            row = self.store.append("mail", id="m-" + uuid.uuid4().hex, task_id=self.task_id,
                                    sender=sender, recipients=targets, mail_kind=kind, text=text, urgent=urgent, **fields)
            self.mail.append({**row, "pending": list(recipients), "delivered": []})
            if self.interrupts and urgent:
                at = time.time()
                for agent in recipients:
                    if agent in self.active and at - self.last_interrupt.get(agent, 0) >= 60:
                        self.last_interrupt[agent] = at
                        self.interrupt_mail[agent] = row["id"]
                        self.signal(agent).set()
            self._cond.notify_all()
        return row

    def _label(self, row):
        sender = row["sender"]
        if sender == "user":
            label = "Message from the requester, sent while the team was working"
        elif sender == "studio":
            label = "Studio notice"
        elif row["mail_kind"] == "decision" and row["recipients"] == [self.primary]:
            label = f"Decision you made while a member was working: {row.get('action', '')}"
            if row.get("in_response_to_sender"):
                label += f" in response to {row['in_response_to_sender']}: {row.get('subject', '')}"
        else:
            label = f"Message from {self.names.get(sender, sender)} ({sender})"
        if row["mail_kind"] == "teammate_answer":
            return f"[{row.get('asked_by', 'A teammate')} asked you: {row.get('question', '')}]\nYou answered: {row['text']}"
        return f"[{label}]\n{row['text']}"



    # called by agent threads
    def ask(self, agent: str, question: str) -> str:
        with self._cond:
            if self._pending is None:
                qid = "q-" + uuid.uuid4().hex[:10]
                self._pending = {"id": qid, "agent": agent, "question": question}
                self._answer = None
                self.questions += 1
                self.store.append("question", task_id=self.task_id, agent=agent, question=question, qid=qid)
            while self._answer is None:
                if self.should_cancel():
                    self._pending = None
                    raise Cancelled()
                if self.signal(agent).is_set():
                    self._pending = None
                    raise InterruptedError("Interrupted by a new message")
                if self.control:
                    self.control.boundary()
                self._cond.wait(timeout=0.5)
            answer, self._answer, self._pending = self._answer, None, None
            return answer

    def note(self, agent: str, text: str) -> None:
        """A Studio notice for one agent (e.g. a soft budget mark), delivered at its next step."""
        if self.enabled:
            self.post("studio", [agent], "notice", text)
        else:
            with self._cond:
                self._notes.setdefault(agent, []).append(text)

    def take(self, agent: str) -> list[str]:
        with self._cond:
            cursor = self._cursors.get(agent, 0)
            new = self._messages[cursor:]
            self._cursors[agent] = len(self._messages)
            notes = self._notes.pop(agent, [])
            notes.extend(f"[Message from the requester, sent while the team was working]\n{text}" for _, text in new)
            receipts = []
            for row in self.mail:
                if agent not in row["pending"]:
                    continue
                row["pending"].remove(agent)
                receipt = {"agent": agent, "mail_id": row["id"], "sender": row["sender"], "mail_kind": row["mail_kind"],
                           "urgent":row["urgent"], "latency_s": max(0., round(time.time() - row["time"], 3)),
                           "interrupted": agent in self.interrupted and self.interrupt_mail.get(agent) == row["id"]}
                row["delivered"].append(receipt)
                self.store.append("mail_receipt", task_id=self.task_id, **receipt)
                receipts.append(receipt)
                notes.append(self._label(row))
            self.signal(agent).clear()
            self.interrupt_mail.pop(agent, None)
            self.interrupted.discard(agent)
        if self.on_event:
            for receipt in receipts:
                self.on_event({"type":"mail_delivered", **receipt})
        return notes

    def start_cursor(self, agent: str, before_seq: int) -> None:
        """An agent that starts now has already seen all earlier messages in its context."""
        with self._cond:
            self._cursors[agent] = sum(seq < before_seq for seq, _ in self._messages)
            self.active.add(agent)
            # Broadcasts newer than this assignment's context snapshot also reach its inbox.
            for row in self.mail:
                if row["recipients"] == ["*"] and row["seq"] >= before_seq and agent not in row["pending"]:
                    if not any(read["agent"] == agent for read in row["delivered"]):
                        row["pending"].append(agent)

    def finish(self) -> None:
        with self._cond:
            self._finished = True

    # called by the service (requester)
    def pending(self) -> dict | None:
        with self._cond:
            return dict(self._pending) if self._pending and self._answer is None else None

    def receive(self, text: str, *, attachments=(), answer_to=None, steer_only=False, client_id=None, recipients=None, urgent=False) -> str:
        """Apply an answer to its exact question, or steer without consuming a question."""
        with self._cond:
            if self._finished or self.should_cancel():
                return "stale"
            pending = self._pending if self._answer is None else None
            if answer_to is not None and (not pending or pending["id"] != answer_to):
                return "stale"
            receipt = {"client_id":client_id} if client_id else {}
            if pending and not steer_only:
                self._answer = text
                self.store.append("answer", task_id=self.task_id, qid=pending["id"], text=text,
                                  agent=pending["agent"], attachments=list(attachments), **receipt)
                self._cond.notify_all()
                return "answer"
            if self.enabled and recipients:
                receipt["recipients"] = list(recipients)
            message = self.store.append("user_message", task_id=self.task_id, text=text, during_task=True,
                              attachments=list(attachments), **receipt)
            if not self.enabled:
                self._messages.append((message["seq"], text))
                return "steering"
            row = self.post("user", recipients or ["*"], "steer", text, urgent=urgent,
                            in_reply_to=message["seq"], attachments=list(attachments))
        if self.on_user_mail:
            self.on_user_mail(row)
        return "steering"
