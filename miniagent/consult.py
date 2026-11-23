"""One serialized lead decision alongside a synchronous hand-off."""
from __future__ import annotations

import copy
import json
import threading
import time
from collections import deque

from .checkpoint import PauseRequested
from .provider_http import ProviderError


def copy_conversation(messages, running):
    """Balance just the open tool batch in a detached snapshot of a live conversation."""
    history = copy.deepcopy(list(messages))
    pending = {}
    for row in history:
        if row["role"] == "assistant":
            pending = {c["id"]: c for c in row.get("tool_calls", [])}
        elif row["role"] == "tool":
            pending.pop(row["tool_call_id"], None)
    for call_id in pending:
        history.append({"role":"tool", "tool_call_id":call_id,
                        "content":f"(still running: {', '.join(running)} is working on this)"})
    return history


class LeadConsult:
    def __init__(self, task):
        self.task = task
        self.gate = threading.Lock()
        self.lock = threading.RLock()
        self.pending = deque()
        self.worker = None
        self.calls = 0
        self.closed = False

    def assignments(self):
        with self.task.lock:
            return {agent: row["seq"] for agent, row in self.task.inflight.items()}

    def schedule(self, mail):
        t = self.task
        if (not t.settings.team.lead_consult or t.primary not in mail["recipients"]
                and "*" not in mail["recipients"]):
            return
        targets = self.assignments()
        if not targets:
            return
        with self.lock:
            if self.closed or t.control.requested.is_set():
                return
            self.pending.append((mail, targets))
            if self.worker is None:
                self.worker = threading.Thread(target=self._drain, name=f"consult-{t.task_id}", daemon=True)
                self.worker.start()

    def _drain(self):
        while True:
            with self.lock:
                if not self.pending or self.closed:
                    self.worker = None
                    return
                mail, targets = self.pending.popleft()
            try:
                self.decide(mail, assignments=targets)
            except PauseRequested:
                # The original steering mail remains in the lead's inbox at its checkpoint.
                with self.lock:
                    self.pending.clear()
                    self.worker = None
                return

    def close(self):
        with self.lock:
            self.closed = True
            self.pending.clear()

    def decide(self, mail, *, checkin=False, assignments=None):
        t = self.task
        assignments = self.assignments() if assignments is None else assignments
        default = {"action":"continue" if checkin else "hold", "to":[], "text":"The lead has your message.",
                   "unavailable":True}
        while not self.gate.acquire(timeout=.1):
            t.control.boundary()
            if self.closed or t.should_cancel():
                return default
        try:
            t.control.boundary()
            active = self.assignments()
            targets = [agent for agent, seq in assignments.items() if active.get(agent) == seq]
            with self.lock:
                if (not targets or self.closed or t.should_cancel() or not t.settings.team.lead_consult
                        or self.calls >= (3 if t.settings.team.fast else 10)):
                    return {**default, "text":"No new lead decision is available; retain the current assignment."}
                self.calls += 1
            history = copy_conversation(t.lead_session.messages, targets)
            if not history:
                return default
            progress = [e["text"][:500] for e in t.store.chat() if e.get("task_id") == t.task_id
                        and e["kind"] == "progress" and e.get("agent") in targets][-6:]
            actions = ("continue, adjust, stop_and_report" if checkin else "forward, forward_urgent, stop_member, reply, hold")
            request = {"from":mail["sender"], "message":mail["text"], "active_members":targets, "recent_progress":progress}
            for key in ("step", "decision", "options"):
                if key in mail:
                    request[key] = mail[key]
            history.append({"role":"user", "content":"Decide as the task lead while the hand-off runs. Return one JSON object "
                            'with "action" (' + actions + '), "to" (list of member IDs) and "text" (answer or instructions).\n'
                            + json.dumps(request, ensure_ascii=False)})
            decision, fallback = self._call(history, checkin, targets, default, mail)
            # No result from a paused or finished worker can reach another assignment after resume.
            with t.lock, self.lock:
                t.control.boundary()
                if self.closed or t.should_cancel():
                    return default
                current = self.assignments()
                if not any(current.get(a) == assignments[a] for a in targets):
                    return default
                dest = [a for a in decision["to"] if a in assignments and current.get(a) == assignments[a]]
                if checkin and decision.get("unavailable"):
                    return decision
                action, text = decision["action"], decision["text"]
                if not checkin and action in ("forward", "forward_urgent", "stop_member"):
                    if fallback and "*" in mail["recipients"]:
                        # Each active member already has the original broadcast.
                        dest = []
                    for agent in dest:
                        if action == "stop_member":
                            t.inflight[agent]["report_request"] = text or "Stop this assignment and send your report."
                    if dest:
                        t.channel.post(t.primary, dest, "decision", text, urgent=action != "forward",
                                       action=action, reply_to=mail["id"])
                if action == "reply":
                    t.store.append("room_reply", task_id=t.task_id, agent=t.primary, text=text,
                                   in_reply_to=mail.get("in_reply_to", mail["seq"]))
                t.channel.post(t.primary, [t.primary], "decision", text, action=action, reply_to=mail["id"],
                               decided_for=dest, fallback=fallback, in_response_to_sender=mail["sender"],
                               subject=mail["text"][:500])
                t._event({"type":"consult","agent":t.primary,"action":action,"mail_id":mail["id"],
                          "latency_s":max(0., round(time.time()-mail["time"], 3)), "fallback":fallback})
                return {**decision, "to":dest}
        finally:
            self.gate.release()

    def _call(self, history, checkin, targets, default, mail):
        t = self.task
        asked = time.monotonic()
        opts = {}
        if getattr(t.provider, "supports_streaming", False):
            opts = {"on_stream": lambda *_: None,
                    "should_cancel":lambda: t.control.requested.is_set() or self.closed or t.should_cancel()}
        try:
            response = t.provider.chat(history, temperature=t.settings.temperature, **opts)
        except ProviderError as exc:
            t._event({"type":"model_call","agent":t.primary,"purpose":"consult","model":t.settings.model,
                      "seconds":round(time.monotonic()-asked, 3), "error":exc.code})
        else:
            usage = response.usage
            t._event({"type":"model_call","agent":t.primary,"purpose":"consult","model":t.settings.model,
                      "seconds":round(time.monotonic()-asked, 3), "prompt_tokens":usage.prompt_tokens,
                      "cached_tokens":usage.prompt_cache_hit_tokens or 0, "output_tokens":usage.completion_tokens})
            try:
                data = json.loads(response.text)
            except ValueError:
                data = None
            actions = ("continue","adjust","stop_and_report") if checkin else ("forward","forward_urgent","stop_member","reply","hold")
            if isinstance(data, dict) and data.get("action") in actions and isinstance(data.get("text"), str):
                dest = data.get("to", [])
                if (isinstance(dest, list) and all(isinstance(a,str) and a in targets for a in dest)
                        and (data["action"] not in ("forward","forward_urgent","stop_member") or dest)
                        and (data["action"] in ("continue","hold") or data["text"].strip())):
                    return {"action":data["action"], "to":list(dict.fromkeys(dest)), "text":data["text"]}, False
        if checkin:
            return {**default, "text":"No decision could be read. The lead has your check-in."}, True
        return {"action":"forward","to":targets,"text":mail["text"]}, True
