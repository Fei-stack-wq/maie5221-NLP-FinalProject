"""Team execution: a lead coordinates members through synchronous delegation.

One *task* = one requester message that starts work in a group. The primary
agent (the group lead, or a member the message @-mentions) runs a normal
:class:`~miniagent.agent.Agent` loop. Its extra tools are the team protocol:

``delegate_task(assignee, task, deliverables)``
    Runs the member's own agent loop to completion *inside the tool call* and
    returns the member's report. This is the "agents as tools" / hierarchical
    manager pattern (OpenAI Agents SDK ``Agent.as_tool``, CrewAI hierarchical
    process, AutoGen Magentic-One orchestrator), see docs/architecture.md.
``ask_human(question)``
    Posts a question to the group chat and waits for the requester's reply.
``remember(note)``
    Appends to the agent's long-term notes for this group.

Files move only through the team project (see :mod:`miniagent.workspace`):
the lead's own edits are published before each delegation, the member checks
out the project, works privately, and its changes are published when it
reports. The lead's workspace is refreshed afterwards.

Every loop is bounded: the lead has ``TeamLimits.lead_max_steps`` turns and
``max_delegations`` assignments; a member has ``Limits.max_steps`` turns;
near-identical repeated assignments are refused after
``max_repeat_assignments`` (Magentic-One's stall counter idea, applied to
delegation). Reports are always returned — a member that hits its budget
still writes a final report (see :class:`~miniagent.agent.Agent`).
"""

from __future__ import annotations

import difflib
import re
import threading
import time
import uuid
from dataclasses import replace
from typing import Any, Callable

from .agent import Agent, AgentResult
from .config import Settings
from .providers import Provider
from .sandbox import Sandbox
from .schemas import ToolResult, Usage
from .store import GroupStore, Member, now
from .tools import Tool, ToolRegistry, build_default_registry
from .workspace import listing

REPORT_LIMIT = 14_000


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
        self._messages: list[str] = []
        self._cursors: dict[str, int] = {}
        self.questions = 0

    # called by agent threads
    def ask(self, agent: str, question: str) -> str:
        with self._cond:
            qid = "q-" + uuid.uuid4().hex[:10]
            self._pending = {"id": qid, "agent": agent, "question": question}
            self._answer = None
            self.questions += 1
            self.store.append("question", task_id=self.task_id, agent=agent, question=question, qid=qid)
            while self._answer is None:
                if self.should_cancel():
                    self._pending = None
                    raise Cancelled()
                self._cond.wait(timeout=0.5)
            answer, self._answer, self._pending = self._answer, None, None
            return answer

    def take(self, agent: str) -> list[str]:
        with self._cond:
            cursor = self._cursors.get(agent, 0)
            new = self._messages[cursor:]
            self._cursors[agent] = len(self._messages)
        return [f"[Message from the requester, sent while the team was working]\n{text}" for text in new]

    def start_cursor(self, agent: str) -> None:
        """An agent that starts now has already seen all earlier messages in its context."""
        with self._cond:
            self._cursors[agent] = len(self._messages)

    # called by the service (requester)
    def pending(self) -> dict | None:
        with self._cond:
            return dict(self._pending) if self._pending else None

    def receive(self, text: str) -> str:
        """Route a requester message: answer the pending question, else queue it. Returns the route."""
        with self._cond:
            if self._pending is not None and self._answer is None:
                qid = self._pending["id"]
                self._answer = text
                self.store.append("answer", task_id=self.task_id, qid=qid, text=text,
                                  agent=self._pending["agent"])
                self._cond.notify_all()
                return "answer"
            self._messages.append(text)
            self.store.append("user_message", task_id=self.task_id, text=text, during_task=True)
            return "steering"


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
def _roster(members: list[Member], exclude: str) -> str:
    rows = []
    for member in members:
        if member.id == exclude:
            continue
        duty = re.sub(r"\s+", " ", member.prompt).strip()
        rows.append(f"- `{member.id}` — {member.name}: {duty[:900] or '(no description)'}")
    return "\n".join(rows)


def lead_prompt(me: Member, members: list[Member], settings: Settings, interactive: bool,
                can_delegate: bool) -> str:
    parts = [
        f"You are {me.name} (id `{me.id}`). {me.prompt}".strip(),
        "",
        "You are working in a group chat with a human requester" + (" and your team." if can_delegate else "."),
    ]
    if can_delegate:
        parts += [
            "You lead this task. Your teammates:",
            _roster(members, me.id),
            "",
            "## How the team works",
            "- `delegate_task` hands one assignment to one teammate. It runs that teammate until they finish and "
            "returns their report — it is synchronous. Plan the work, then delegate pieces in the order they are "
            "needed, building on each report.",
            "- Every agent has a private workspace that starts as a copy of the shared team project. When a "
            "teammate finishes, the files they created, changed or deleted are published to the team project "
            "automatically. Your workspace is refreshed after every delegation; your own edits are published "
            "before each delegation and when you finish.",
            "- Write each assignment so a capable colleague can execute it without asking you: the goal, the "
            "relevant context and decisions so far (teammates also see the team chat), exact file paths to "
            "create or change, and what 'done' means — how it will be checked.",
            "- Use each teammate for what their role is for. Important work should be checked by a teammate other "
            "than its author (tests, fact-checks, risk review…). When a check finds real problems, send the "
            "concrete findings back to the author to fix, then have the fix re-checked. Stop iterating once the "
            "result is good — do not polish forever or repeat the same assignment.",
            "- You may read files and run commands yourself to verify reports before you rely on them, and you "
            "may write files yourself when that is part of your own role.",
        ]
    else:
        parts += [
            "",
            "## How you work",
            "- You have a private workspace that is a copy of the shared project; files you create or change are "
            "published to the project when you finish.",
            "- Do the work yourself with your tools and verify it before you finish.",
        ]
    if interactive:
        parts += [
            "- Use `ask_human` when a decision genuinely belongs to the requester: goals, audience, preferences, "
            "scope trade-offs, private information. Ask early, combine related questions into one message, and "
            "do not ask about things the team can decide or find out itself. Questions in a final answer are not "
            "seen until the task is over — ask with `ask_human` instead.",
        ]
    else:
        parts += [
            "- The requester is not available during this task. Make reasonable, clearly stated assumptions "
            "instead of asking, and list them in your final answer.",
        ]
    parts += [
        "- Save durable lessons, conventions and requester preferences with `remember`.",
        "",
        "## Finishing",
        "End with your final message to the requester (plain text, no tool call): what was delivered (file "
        "paths in the team project), how to open or use it, key decisions and assumptions, how it was verified, "
        "and known limitations. Write in the language the requester used in their message (English if they wrote "
        "English), even though tool outputs may be in another language.",
        "",
        f"Budget for this task: up to {settings.team.lead_max_steps if can_delegate else settings.limits.max_steps} "
        "of your own steps"
        + (f" and {settings.team.max_delegations} delegations." if can_delegate else "."),
    ]
    return "\n".join(parts)


def member_prompt(me: Member, lead: Member, members: list[Member], settings: Settings, interactive: bool) -> str:
    parts = [
        f"You are {me.name} (id `{me.id}`). {me.prompt}".strip(),
        "",
        f"You are a member of a team led by {lead.name} (`{lead.id}`), working with a human requester. "
        "Other teammates:",
        _roster(members, me.id) or "(none)",
        "",
        "## How you work",
        "- You are now working on one assignment from the lead. Your workspace is a private copy of the shared "
        "team project. Everything you create, change or delete there is published to the team when you finish.",
        "- Work like a professional in your role: inspect the existing files first, do the work completely, and "
        "verify it before reporting — run the code and tests, open pages with run_browser, check sources.",
        "- Do not leave throwaway files in the project. Keep tests or evidence the team should keep in sensible "
        "places (e.g. tests/, docs/); delete scratch files before you finish.",
    ]
    if interactive:
        parts.append("- Use `ask_human` only for decisions that only the requester can make. Otherwise make a "
                     "reasonable assumption and state it in your report.")
    else:
        parts.append("- The requester is not available. Make reasonable assumptions and state them in your report.")
    parts += [
        "- Save durable lessons and conventions with `remember`.",
        "",
        "## Your report",
        "When you are done, reply with your report as plain text (no tool call): what you did, the files you "
        "created or changed, how you verified it (commands and results), and open issues or risks. Be specific "
        "and concise. Write in the language of the assignment (English if the assignment is in English).",
        "",
        f"Budget for this assignment: up to {settings.limits.max_steps} steps.",
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Context from the group's history
# ---------------------------------------------------------------------------
def _clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    return text[:head] + f"\n…[{len(text) - limit} characters omitted]…\n" + text[-(limit - head):]


def render_event(event: dict, names: dict[str, str]) -> str:
    kind = event["kind"]
    who = lambda agent: f"{names.get(agent, agent)} ({agent})"  # noqa: E731
    if kind == "user_message":
        return f"**Requester:** {_clip(event['text'], 6000)}"
    if kind == "delegation":
        files = f" — expected files: {', '.join(event['deliverables'])}" if event.get("deliverables") else ""
        return f"**{who(event['agent'])} → {who(event['assignee'])}:** {_clip(event['task'], 2500)}{files}"
    if kind == "report":
        files = ", ".join(f"{c['path']} ({c['change']})" for c in event.get("changes", [])[:30])
        return (f"**{who(event['agent'])} reported ({event['status']}):** {_clip(event['text'], 3500)}"
                + (f"\nPublished: {files}" if files else ""))
    if kind == "question":
        return f"**{who(event['agent'])} asked the requester:** {_clip(event['question'], 2000)}"
    if kind == "answer":
        return f"**Requester answered:** {_clip(event['text'], 3000)}"
    if kind == "final":
        return f"**{who(event['agent'])} (final answer, task {event['status']}):** {_clip(event['text'], 5000)}"
    if kind == "task_finished":
        return f"— task finished: {event['status']} —"
    return ""


def conversation(store: GroupStore, before_seq: int, budget_chars: int = 48_000) -> str:
    names = {m.id: m.name for m in store.group.members}
    lines: list[str] = []
    used = 0
    for event in reversed(store.chat()):
        if event["seq"] >= before_seq:
            continue
        line = render_event(event, names)
        if not line:
            continue
        if used + len(line) > budget_chars:
            lines.append("…(earlier conversation omitted)…")
            break
        lines.append(line)
        used += len(line)
    return "\n\n".join(reversed(lines))


def agent_context(store: GroupStore, agent_id: str, before_seq: int) -> str:
    sections = []
    chat = conversation(store, before_seq)
    if chat:
        sections.append("## Group conversation so far\n" + chat)
    sections.append("## Team project files\n" + listing(store.project.files()))
    memory = store.memory(agent_id).strip()
    if memory:
        sections.append("## Your notes from earlier work in this group\n" + _clip(memory, 4000))
    journal = store.journal(agent_id, limit=5)
    if journal:
        rows = [f"- ({j.get('status')}) {_clip(j.get('assignment', ''), 300)} → files: "
                f"{', '.join(j.get('files', [])[:10]) or 'none'}" for j in journal]
        sections.append("## Your recent assignments in this group\n" + "\n".join(rows))
    sections.append(f"Current date and time: {time.strftime('%Y-%m-%d %H:%M %Z')}.")
    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# One task
# ---------------------------------------------------------------------------
class TeamTask:
    def __init__(self, store: GroupStore, provider: Provider, settings: Settings, *, task_id: str, text: str,
                 primary: str, trigger_seq: int, channel: HumanChannel,
                 should_cancel: Callable[[], bool], on_activity: Callable[[dict], None] | None = None) -> None:
        self.store = store
        self.provider = provider
        self.settings = settings
        self.task_id = task_id
        self.text = text
        self.primary = primary
        self.trigger_seq = trigger_seq
        self.channel = channel
        self.should_cancel = should_cancel
        self.on_activity = on_activity
        self.group = store.group
        self.interactive = self.group.mode == "interactive"
        self.delegations = 0
        self.assignments: list[dict] = []
        self.history: dict[str, list[str]] = {}
        self.lead_baseline: dict[str, str] = {}
        self.active: list[str] = []
        self.record: dict[str, Any] = {}

    # -- plumbing ---------------------------------------------------------
    def _event(self, event: dict) -> None:
        event = {"task_id": self.task_id, **event}
        if event["type"] != "delta":
            self.store.trace(self.task_id, event)
        if self.on_activity:
            self.on_activity(event)

    def _registry(self, agent_id: str, *, lead: bool) -> ToolRegistry:
        registry = build_default_registry()
        if lead and len(self.group.members) > 1:
            registry.add(self._delegate_tool(agent_id))
        if self.interactive:
            registry.add(Tool(
                name="ask_human",
                description=("Ask the human requester a question in the group chat and wait for the answer. "
                             "Use it for decisions only the requester can make; put all related questions in one message."),
                parameters={"type": "object", "properties": {
                    "question": {"type": "string", "description": "The complete question(s) for the requester"}},
                    "required": ["question"]},
                fn=lambda _ctx, args, agent=agent_id: self._ask(agent, args["question"])))
        registry.add(Tool(
            name="remember",
            description=("Save a short note to your long-term memory in this group (requester preferences, "
                         "decisions, conventions, lessons). Your notes are shown to you in every future assignment."),
            parameters={"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]},
            fn=lambda _ctx, args, agent=agent_id: self._remember(agent, args["note"])))
        return registry

    def _ask(self, agent: str, question: str) -> ToolResult:
        question = question.strip()
        if not question:
            return ToolResult.failure("The question is empty.")
        self._event({"type": "question", "agent": agent, "question": question})
        try:
            answer = self.channel.ask(agent, question)
        except Cancelled:
            return ToolResult.failure("The task was cancelled while waiting for the requester.")
        self._event({"type": "answer", "agent": agent, "text": answer})
        return ToolResult.success(f"The requester answered:\n{answer}")

    def _remember(self, agent: str, note: str) -> ToolResult:
        note = note.strip()
        if not note or len(note) > 2000:
            return ToolResult.failure("A note must be 1–2000 characters.")
        self.store.remember(agent, note)
        return ToolResult.success("Saved to your long-term notes.")

    # -- delegation -------------------------------------------------------
    def _delegate_tool(self, lead_id: str) -> Tool:
        ids = [m.id for m in self.group.members if m.id != lead_id]
        return Tool(
            name="delegate_task",
            description=("Give one assignment to one teammate and wait for their report. The teammate works in a "
                         "private copy of the team project; their file changes are published to the project when "
                         "they finish. Returns their report and the list of published files."),
            parameters={"type": "object", "properties": {
                "assignee": {"type": "string", "enum": ids, "description": "Teammate id"},
                "task": {"type": "string", "description": "The complete assignment: goal, context, exact files, "
                                                          "and how the result will be checked"},
                "deliverables": {"type": "array", "items": {"type": "string"},
                                 "description": "Project-relative paths the teammate should create or update"},
            }, "required": ["assignee", "task"]},
            fn=lambda _ctx, args: self._delegate(lead_id, args))

    def _delegate(self, lead_id: str, args: dict) -> ToolResult:
        assignee = str(args.get("assignee", "")).strip()
        task = str(args.get("task", "")).strip()
        deliverables = [str(p).strip() for p in args.get("deliverables") or [] if str(p).strip()][:40]
        valid = [m.id for m in self.group.members if m.id != lead_id]
        if assignee not in valid:
            return ToolResult.failure(f"Unknown assignee {assignee!r}. Choose one of: {', '.join(valid)}.")
        if len(task) < 10:
            return ToolResult.failure("The assignment is empty or too short. Describe the work completely.")
        if self.delegations >= self.settings.team.max_delegations:
            return ToolResult.failure(
                f"The delegation budget for this task ({self.settings.team.max_delegations}) is used up. "
                "Finish now with what the team has produced and state what remains open.")
        previous = self.history.setdefault(assignee, [])
        similar = sum(1 for old in previous
                      if difflib.SequenceMatcher(None, old[:3000], task[:3000]).ratio() >= 0.9)
        if similar >= self.settings.team.max_repeat_assignments:
            return ToolResult.failure(
                f"Refused: you have already given {assignee} this assignment {similar} times with nearly the same "
                "wording. Repeating it will not change the outcome. Use the previous reports, change the approach "
                "(different instructions, a different teammate, or doing it yourself), or finish and report what "
                "is still open.")
        self.delegations += 1
        previous.append(task)
        member = self.group.member(assignee)
        lead_workspace = self.store.workspace(lead_id)
        own, _ = self.store.project.publish(lead_workspace, self.lead_baseline, author=lead_id,
                                            task_id=self.task_id, note="lead edits before delegation")
        if own:
            self.store.append("publish", task_id=self.task_id, agent=lead_id, changes=[c.as_dict() for c in own])
        event = self.store.append("delegation", task_id=self.task_id, agent=lead_id, assignee=assignee,
                                  task=task, deliverables=deliverables)
        result, changes, skipped = self._run_member(member, task, deliverables, event["seq"])
        self.lead_baseline = self.store.project.checkout(lead_workspace)
        report = result.answer or "(no report)"
        if result.error:
            report += f"\n\n[Run problem: {result.error}]"
        self.store.append("report", task_id=self.task_id, agent=assignee, status=result.status, text=report,
                          changes=[c.as_dict() for c in changes], steps=result.steps, tool_calls=result.tool_calls)
        lines = [f"Report from {member.name} ({assignee}) — status: {result.status}"
                 + ("" if result.status == "completed" else " (the assignment did not finish normally)"),
                 "", _clip(report, REPORT_LIMIT), ""]
        if changes:
            lines.append("Published to the team project: " + ", ".join(
                f"{c.path} ({c.change}{', overwrote a concurrent change' if c.conflict else ''})" for c in changes))
        else:
            lines.append("No files were changed by this assignment.")
        manifest = self.store.project.manifest()
        def present(path: str) -> bool:
            path = path.strip().removeprefix("./").rstrip("/")
            return path in manifest or any(name.startswith(path + "/") for name in manifest)
        missing = [p for p in deliverables if not present(p)]
        if missing:
            lines.append("Expected files that do not exist in the project: " + ", ".join(missing))
        if skipped:
            lines.append("Not published (skipped): " + ", ".join(skipped[:20]))
        lines.append(f"Delegations used: {self.delegations}/{self.settings.team.max_delegations}.")
        ok = result.status in {"completed", "max_steps", "stuck", "budget"}
        return ToolResult(ok=ok, content="\n".join(lines))

    def _run_member(self, member: Member, task: str, deliverables: list[str], trigger_seq: int):
        workspace = self.store.workspace(member.id)
        baseline = self.store.project.checkout(workspace)
        lead = self.group.member(self.primary)
        assignment = task
        if deliverables:
            assignment += "\n\nFiles you are expected to create or update: " + ", ".join(deliverables)
        assignment = f"Assignment from {lead.name} ({lead.id}):\n\n" + assignment
        agent = Agent(member.id, self.provider, self._registry(member.id, lead=False), Sandbox(workspace, member.id),
                      self.settings, system_prompt=member_prompt(member, lead, self.group.members, self.settings,
                                                                 self.interactive),
                      on_event=self._event, should_cancel=self.should_cancel, inbox=self.channel,
                      stream=True)
        self.channel.start_cursor(member.id)
        self.active.append(member.id)
        self._update(active=list(self.active))
        try:
            result = agent.run(assignment, context=agent_context(self.store, member.id, trigger_seq))
        finally:
            self.active.remove(member.id)
            self._update(active=list(self.active))
        changes, skipped = self.store.project.publish(workspace, baseline, author=member.id, task_id=self.task_id,
                                                      note=task[:300])
        self.assignments.append(_metrics(member.id, result))
        self.store.record_assignment(member.id, {
            "task_id": self.task_id, "assignment": task[:1500], "status": result.status,
            "files": [c.path for c in changes], "report": (result.answer or "")[:1500]})
        self._update(assignments=self.assignments, delegations=self.delegations)
        return result, changes, skipped

    # -- the task ---------------------------------------------------------
    def _update(self, **fields: Any) -> None:
        self.record.update(fields)
        self.store.save_task(self.record)

    def run(self) -> dict:
        started = time.monotonic()
        me = self.group.member(self.primary)
        can_delegate = self.primary == self.group.lead and len(self.group.members) > 1
        self.record = {"id": self.task_id, "group_id": self.group.id, "text": self.text, "primary": self.primary,
                       "status": "running", "created_at": now(), "finished_at": None, "active": [self.primary],
                       "delegations": 0, "assignments": [], "final_answer": "", "error": ""}
        self.store.save_task(self.record)
        self.store.append("task_started", task_id=self.task_id, agent=self.primary)
        workspace = self.store.workspace(self.primary)
        self.lead_baseline = self.store.project.checkout(workspace)
        settings = self.settings
        # The coordinating lead gets the lead budget; a member addressed directly works
        # like any assignment, with the member budget.
        limits = replace(settings.limits, max_steps=settings.team.lead_max_steps) if can_delegate else settings.limits
        prompt = lead_prompt(me, self.group.members, settings, self.interactive, can_delegate)
        if not can_delegate and self.primary != self.group.lead:
            lead = self.group.member(self.group.lead)
            prompt += (f"\n\nThe requester addressed this message to you directly, so you handle it yourself. "
                       f"The team lead is {lead.name} ({lead.id}).")
        agent = Agent(self.primary, self.provider, self._registry(self.primary, lead=can_delegate),
                      Sandbox(workspace, self.primary), settings, system_prompt=prompt, limits=limits,
                      on_event=self._event, should_cancel=self.should_cancel, inbox=self.channel, stream=True)
        self.channel.start_cursor(self.primary)
        result = agent.run(self.text, context=agent_context(self.store, self.primary, self.trigger_seq))
        changes, _ = self.store.project.publish(workspace, self.lead_baseline, author=self.primary,
                                                task_id=self.task_id, note="final edits")
        if changes:
            self.store.append("publish", task_id=self.task_id, agent=self.primary,
                              changes=[c.as_dict() for c in changes])
        status = {"completed": "completed", "cancelled": "cancelled", "error": "failed"}.get(result.status, "incomplete")
        answer = result.answer or ("" if status == "cancelled" else "(The lead stopped without a final answer.)")
        if answer:
            self.store.append("final", task_id=self.task_id, agent=self.primary, text=answer, status=status,
                              error=result.error)
        totals = _metrics(self.primary, result)
        usage = Usage(prompt_tokens=totals["prompt_tokens"], completion_tokens=totals["completion_tokens"])
        for item in self.assignments:
            usage = usage.merged(Usage(prompt_tokens=item["prompt_tokens"], completion_tokens=item["completion_tokens"]))
        metrics = {"duration_s": round(time.monotonic() - started, 1), "lead": totals,
                   "delegations": self.delegations, "questions": self.channel.questions,
                   "prompt_tokens": usage.prompt_tokens, "completion_tokens": usage.completion_tokens,
                   "steps": totals["steps"] + sum(a["steps"] for a in self.assignments),
                   "tool_calls": totals["tool_calls"] + sum(a["tool_calls"] for a in self.assignments)}
        self._update(status=status, finished_at=now(), final_answer=answer, error=result.error,
                     active=[], metrics=metrics, assignments=self.assignments, delegations=self.delegations)
        self.store.append("task_finished", task_id=self.task_id, status=status, metrics=metrics, error=result.error)
        return self.record


def _metrics(agent: str, result: AgentResult) -> dict:
    return {"agent": agent, "status": result.status, "steps": result.steps, "tool_calls": result.tool_calls,
            "prompt_tokens": result.usage.prompt_tokens, "completion_tokens": result.usage.completion_tokens,
            "duration_s": result.duration_s, "compactions": result.compactions, "error": result.error}
