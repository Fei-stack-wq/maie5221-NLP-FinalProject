"""Task-scoped check-ins and read-only questions between members of one team."""
from __future__ import annotations

import threading
from dataclasses import replace

from .agent import Agent, AgentSession
from .consult import copy_conversation
from .context import ContextManager
from .checkpoint import PauseRequested
from .sandbox import Sandbox
from .schemas import ToolResult
from .tools import Tool, build_default_registry


def enabled(settings):
    return settings.team.team_talk and settings.team.mailboxes


def fork_history(source, settings, prompt, member):
    """Build a detached answer session. An in-flight tool batch belongs to its original runner."""
    if source is None or not source.messages:
        return None
    messages = copy_conversation(source.messages, [member])
    messages[0] = {"role": "system", "content": prompt}
    if messages[-1]["role"] == "user":
        # The live teammate may still be answering the last request.
        messages.append({"role": "assistant", "content": "(No response recorded at this snapshot.)",
                         "reasoning_content": ""})
    return AgentSession(messages=messages, context=ContextManager(settings.compact_at_tokens,
                                                                  settings.keep_recent_tokens))


class TeamTalk:
    def __init__(self, team):
        self.team = team
        self.timers = {}
        self.open_questions = set()

    def tools(self, sender):
        peers = [member.id for member in self.team.group.members if member.id not in (sender, self.team.primary)]
        member = {"type": "string", "enum": peers}
        phrase = {"type": "string", "minLength": 1, "maxLength": 3000}

        def tool(name, description, props, required, fn):
            def execute(ctx, args):
                if "member" in args and args["member"] not in peers:
                    return ToolResult.failure(f"Choose a teammate: {', '.join(peers)}. Reach the lead with check_in.")
                if any(not v.strip() or len(v) > props[k].get("maxLength", 3000)
                       for k, v in args.items() if isinstance(v, str)):
                    return ToolResult.failure("Use non-empty text within the advertised field lengths.")
                options = args.get("options", [])
                if len(options) > 8 or any(not isinstance(v, str) or not v.strip() or len(v) > 3000 for v in options):
                    return ToolResult.failure("Pass at most eight non-empty options of up to 3000 characters each.")
                return fn(sender, args, ctx)
            return Tool(name, description, {"type": "object", "properties": props, "required": required}, execute)

        result = []
        if self.team.settings.team.lead_consult and self.team.settings.team.check_ins:
            result.append(tool("check_in", "Send a milestone, blocker or decision to the lead and receive their direction.",
                               {"summary": phrase, "decision": phrase, "options": {"type": "array", "maxItems": 8,
                                "items": phrase}, "step": {"type": "string", "maxLength": 60}},
                               ["summary"], self.check_in))
        if peers:
            result.extend([
                tool("ask_teammate", "Ask a teammate a factual question in a short read-only turn.",
                     {"member": member, "question": phrase, "context": phrase}, ["member", "question"], self.ask),
                tool("message_teammate", "Leave a teammate a note for their next step.",
                     {"member": member, "text": phrase}, ["member", "text"], self.note),
            ])
        return result

    def _reserve(self, sender, counter):
        with self.team.lock:
            entry = self.team.inflight.get(sender)
            if entry is None:
                return None, ToolResult.failure("Team communication requires an active member assignment.")
            counts = entry.setdefault("talk", {})
            limit = getattr(self.team.settings.team, counter)
            if counts.get(counter, 0) >= limit:
                return None, ToolResult.failure(f"This assignment has used all {limit} {counter.replace('_', ' ')}.")
            counts[counter] = counts.get(counter, 0) + 1
            return entry, None

    def _trace(self, kind, sender, entry, **fields):
        self.team._event({"type": kind, "agent": sender, "assignment_seq": entry["seq"], **fields})

    def note(self, sender, args, ctx):
        peer, text = args["member"], args["text"].strip()
        if not text:
            return ToolResult.failure("A note needs text.")
        entry, error = self._reserve(sender, "teammate_notes")
        if error:
            return error
        mail = self.team.channel.post(sender, [peer], "note", text, assignment_seq=entry["seq"])
        self._trace("teammate_note", sender, entry, target=peer, mail_id=mail["id"])
        return ToolResult(ok=True, content=f"Note sent to {peer}.")

    def check_in(self, sender, args, ctx):
        summary = args["summary"].strip()
        if not summary:
            return ToolResult.failure("A check-in needs a summary.")
        entry = self.team.inflight[sender]
        mail = entry.get("open_checkin")
        if mail is None:
            entry, error = self._reserve(sender, "check_ins")
            if error:
                return error
            text = summary
            if args.get("decision"):
                text += "\nDecision needed: " + args["decision"]
            if args.get("options"):
                text += "\nOptions: " + " / ".join(args["options"])
            mail = self.team.channel.post(sender, [self.team.primary], "checkin", text, queue_to=[], assignment_seq=entry["seq"],
                                          **{k: args[k] for k in ("summary", "step", "decision", "options") if k in args})
            entry["open_checkin"] = mail
            self._trace("checkin", sender, entry, mail_id=mail["id"], step=args.get("step"))
        try:
            decision = self.team.consult_lead(mail, checkin=True)
        except PauseRequested:
            raise
        except Exception:
            entry.pop("open_checkin", None)
            raise
        entry.pop("open_checkin", None)
        action, text = decision["action"], decision["text"]
        if decision.get("unavailable"):
            self.team.channel.post("studio", [sender], "decision", text, reply_to=mail["id"],
                                   queue_to=[], status="unavailable", action="unavailable")
            return ToolResult(ok=False, content="No lead decision: " + text)
        if action == "stop_and_report":
            entry["report_request"] = text or "The lead requested a progress report now."
        return ToolResult(ok=True, content=f"Lead decision: {action}\n{text}")

    def ask(self, sender, args, ctx):
        target = args["member"]
        question = args["question"].strip()
        if not question:
            return ToolResult.failure("A teammate question needs text.")
        with self.team.lock:
            if sender in self.open_questions:
                return ToolResult.failure("This assignment already has an open teammate question.")
            self.open_questions.add(sender)
        try:
            entry = self.team.inflight[sender]
            mail = entry.get("open_peer_question")
            if mail is None:
                entry, error = self._reserve(sender, "teammate_questions")
                if error:
                    return error
                mail = self.team.channel.post(sender, [target], "teammate_question", question, queue_to=[],
                                              context=args.get("context", ""), assignment_seq=entry["seq"])
                entry["open_peer_question"] = mail
                self._trace("teammate_question", sender, entry, target=target, mail_id=mail["id"])
            try:
                answer, ok, source = self._answer(sender, target, mail, ctx)
            except PauseRequested:
                raise
            except Exception as exc:
                # Close the exact question even if a fork cannot be constructed.
                answer, ok, source = f"Could not answer the question ({type(exc).__name__}).", False, "unavailable"
            # A pause discards this read-only fork. The checkpoint retains the question ID and quota slot.
            self.team.control.boundary()
            reply = self.team.channel.post(target, [sender], "teammate_answer", answer, queue_to=[target],
                                           question=question, asked_by=sender, reply_to=mail["id"], source=source,
                                           assignment_seq=entry["seq"], ok=ok)
            entry.pop("open_peer_question", None)
            self._trace("teammate_answer", target, entry, target=sender, mail_id=reply["id"], reply_to=mail["id"], ok=ok)
            return ToolResult(ok=ok, content=f"{target} ({source}):\n{answer}")
        finally:
            with self.team.lock:
                self.open_questions.discard(sender)

    def _answer(self, sender, target, mail, ctx):
        team = self.team
        member = team.group.member(target)
        prompt = (f"You are {member.name} (id `{target}`). {member.prompt}\n"
                  "Answer your teammate's factual question briefly from the available evidence.")
        with team.lock:
            active = team.inflight.get(target) or {}
            kind = active.get("talk_session_kind")
            if kind is None:
                keys = [key for key in team.sessions if key[0] == target]
                key = max(keys, key=lambda k: team.session_marks.get(k, {}).get("seq", 0), default=(target, "work"))
            else:
                key = (target, kind)
            original = team.sessions.get(key)
            source_seq = active.get("seq") or team.session_marks.get(key, {}).get("seq")
        review = key[1] == "review"
        settings = team.settings.reviewer() if review else team.settings.member()
        session = fork_history(original, settings, prompt, target)
        source = f"{key[1]} session for assignment #{source_seq}, snapshot at message #{mail['seq']}" if session else (
            f"role and room context through message #{mail['seq'] - 1}")
        root = team.store.workspace(target) if session else team.store.project.root
        provider = (team.reviewer_provider if review else team.member_provider) or team.provider

        def observe(event):
            if event["type"] == "model_call":
                team._event({**event, "purpose": "teammate_answer", "mail_id": mail["id"], "asked_by": sender})
            elif event["type"] in ("tool_call", "tool_result"):
                team._event({**event, "type": "teammate_lookup", "phase": event["type"], "mail_id": mail["id"]})

        # One slot is reserved for Agent's final no-tools response at exhaustion.
        fork = Agent(target, provider, build_default_registry(read_only=True), Sandbox(root, target), settings,
                     limits=team._time_boxed(replace(settings.limits, max_steps=min(settings.limits.max_steps, 7))),
                     system_prompt=prompt, on_event=observe, stream=True,
                     should_cancel=lambda: team.control.requested.is_set() or bool(ctx.should_cancel and ctx.should_cancel()))
        question = f"{sender} asks: {mail['text']}"
        if mail.get("context"):
            question += "\nContext: " + mail["context"]
        context = team._context(target, mail["seq"]) if not session else ""
        fork_session = session or AgentSession()
        result = fork.run(question, context=context, session=fork_session)
        last = fork_session.messages[-1] if fork_session.messages else {}
        answered = (result.status not in ("error", "cancelled") and bool(result.answer)
                    and last.get("role") == "assistant" and not last.get("tool_calls")
                    and (last.get("content") or "").strip() == result.answer)
        answer = result.answer if answered else (result.error or "The teammate returned no complete answer.")
        return answer[:6000], answered, source

    def start(self, member, entry, review):
        entry["talk_session_kind"] = "review" if review else "work"
        if not self.team.settings.team.lead_consult or not self.team.settings.team.check_ins:
            return
        with self.team.lock:
            counters = entry.get("talk", {})
            if counters.get("check_ins") or counters.get("silence_prompted"):
                return
            elapsed = entry.get("silence_elapsed", 0.0)
            duration = self.team.settings.team.silence_minutes * 60
            timer = threading.Timer(max(0.0, duration - elapsed), self.silence, args=(member, entry["seq"]))
            timer.daemon = True
            self.timers[member] = (entry, self.team.meter.clock(), timer)
            timer.start()

    def finish(self, member, entry):
        with self.team.lock:
            run = self.timers.pop(member, None)
            if run:
                run[2].cancel()
                entry["silence_elapsed"] = entry.get("silence_elapsed", 0.0) + max(0, self.team.meter.clock() - run[1])

    def silence(self, member, seq=None):
        with self.team.lock:
            run = self.timers.get(member)
            if not run or self.team.control.requested.is_set() or self.team.should_cancel():
                return
            entry = run[0]
            if seq is not None and seq != entry["seq"]:
                return
            counts = entry.setdefault("talk", {})
            if counts.get("check_ins") or counts.get("silence_prompted"):
                return
            counts["silence_prompted"] = True
            self.team.channel.post("studio", [member], "notice",
                                   "Send a short check_in on progress, what remains and any decision needed.",
                                   assignment_seq=entry["seq"])
        self._trace("silence_notice", member, entry)
