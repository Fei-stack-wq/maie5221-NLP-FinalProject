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

import json
import difflib
import re
import threading
import time
import uuid
from dataclasses import asdict, replace
from typing import Any, Callable

from .agent import Agent, AgentResult, AgentSession
from .checkpoint import Continuation, PauseRequested
from .config import Settings
from .context import ContextManager
from .evaluation import evaluate, validate
from .providers import Provider
from .sandbox import Sandbox
from . import recovery
from .schemas import ToolResult, Usage
from .store import GroupStore, Member, now
from .tools import Tool, ToolRegistry, build_default_registry
from .workspace import listing

REPORT_LIMIT = 14_000

CHECKS_SCHEMA = {
    "type": "array", "maxItems": 20,
    "description": ("Mechanical checks Studio runs on the published project after the work, for your information: "
                    "{kind: exists, path} | {kind: contains, path, text} | {kind: command, command} (passes on exit 0)"),
    "items": {"type": "object", "properties": {
        "kind": {"type": "string", "enum": ["exists", "contains", "command"]},
        "path": {"type": "string"}, "text": {"type": "string"}, "command": {"type": "string"}},
        "required": ["kind"]},
}
VERDICT_LINE = re.compile(r"^\W*VERDICT:\s*(APPROVED|CHANGES\s+REQUESTED)\b.*$", re.IGNORECASE | re.MULTILINE)


def _verdict(report: str) -> str:
    """The reviewer's verdict marker, as AutoGen's TextMentionTermination looks for its stop text."""
    match = VERDICT_LINE.search(report[:2000])
    if match is None:
        return "unclear"
    return "approved" if match.group(1).upper() == "APPROVED" else "changes requested"


def check_lines(evaluation: dict) -> list[str]:
    lines = [f"Checks run by Studio on the published project (information, not a gate): "
             f"{evaluation['passed']}/{evaluation['total']} passed"]
    for row in evaluation["checks"]:
        if row["kind"] == "command":
            what = f"command `{_clip(row['command'], 200)}`" + ("" if row["passed"] else f" (exit {row.get('exit_code')})")
        elif row["kind"] == "contains":
            what = f"{row['path']} contains {_clip(row['text'], 80)!r}"
        else:
            what = f"{row['path']} exists"
        lines.append(f"- {'✓' if row['passed'] else '✗'} {what}")
        detail = row.get("error") or ("" if row["passed"] else row.get("output", ""))
        if detail:
            lines.append("  " + _clip(detail, 900).replace("\n", "\n  "))
    return lines


def _norm(path: Any) -> str:
    return str(path).strip().removeprefix("./").rstrip("/")


def _overlaps(a: str, b: str) -> bool:
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _present(path: str, manifest: dict[str, str]) -> bool:
    path = _norm(path)
    return path in manifest or any(name.startswith(path + "/") for name in manifest)


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
        self.questions = 0
        self.control = None

    def snapshot(self):
        with self._cond:
            return {"pending":self._pending, "answer":self._answer, "messages":list(self._messages),
                    "cursors":dict(self._cursors), "questions":self.questions}

    def restore(self, saved):
        self._pending, self._answer = saved["pending"], saved["answer"]
        self._messages, self._cursors, self.questions = saved["messages"], saved["cursors"], saved["questions"]


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
                if self.control:
                    self.control.boundary()
                self._cond.wait(timeout=0.5)
            answer, self._answer, self._pending = self._answer, None, None
            return answer

    def take(self, agent: str) -> list[str]:
        with self._cond:
            cursor = self._cursors.get(agent, 0)
            new = self._messages[cursor:]
            self._cursors[agent] = len(self._messages)
        return [f"[Message from the requester, sent while the team was working]\n{text}" for _, text in new]

    def start_cursor(self, agent: str, before_seq: int) -> None:
        """An agent that starts now has already seen all earlier messages in its context."""
        with self._cond:
            self._cursors[agent] = sum(seq < before_seq for seq, _ in self._messages)

    def finish(self) -> None:
        with self._cond:
            self._finished = True

    # called by the service (requester)
    def pending(self) -> dict | None:
        with self._cond:
            return dict(self._pending) if self._pending and self._answer is None else None

    def receive(self, text: str, *, attachments=(), answer_to=None, steer_only=False, client_id=None) -> str:
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
            message = self.store.append("user_message", task_id=self.task_id, text=text, during_task=True,
                              attachments=list(attachments), **receipt)
            self._messages.append((message["seq"], text))
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
                can_delegate: bool, *, studio_assistant: bool = False) -> str:
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
        if settings.team.parallel_delegations:
            parts.append(
                "- Assignments that do not depend on each other can run at the same time: call `delegate_task` "
                "several times in one response, each with its own `deliverables` and no file shared between them. "
                "Otherwise they run one after another.")
        parts.append(
            "- Add `checks` to an assignment when 'done' can be checked mechanically (a file exists, contains a "
            "phrase, a command exits 0). Studio runs them on the published project after the teammate finishes and "
            "shows you the results; they inform your judgement and never block delivery. Set `review: true` on "
            "an assignment that reviews or tests someone else's work.")
        if settings.team.review_loops and len(members) > 2:
            parts.append(
                "- For an author–reviewer cycle (build → test → fix → re-test), `review_loop` runs the rounds for you: "
                "the reviewer's findings go straight to the author's continued session, and you receive the final "
                "verdict, the last review and the files changed.")
        if settings.team.plans:
            parts.append(
                "- Keep a short plan with `update_plan`: write it once you understand the task and update it after each "
                "report (mark steps done, add or change steps). The requester sees it and may steer you.")
        if settings.team.resume_members:
            parts.append(
                "- A teammate you assign again during this task continues their earlier session: they still "
                "remember what they read, did and reported, and are told which files others changed since. A "
                "follow-up can therefore be short — the concrete findings to fix or the next step — instead of "
                "repeating the background. Set `fresh: true` when you want an independent check that must not "
                "be anchored by that teammate's earlier work in this task.")
    else:
        parts += [
            "",
            "## How you work",
            "- You have a private workspace that is a copy of the shared project; files you create or change are "
            "published to the project when you finish.",
            ("- Decide how to fulfill this commission from the user's intent, prior conversation and available "
             "roles and rooms. You can work here, dispatch to a role's DM or an existing room, or organize a "
             "group and dispatch to it. Use the directory to identify the right destination or role; follow the "
             "user's choice when given. Studio delivers dispatch links and the eventual result and files to this "
             "chat automatically. Use await_result only when your own next step needs that result." if studio_assistant else
             "- Do the work yourself with your tools and verify it before you finish."),
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
        "- Save durable lessons, conventions and requester preferences with `propose_knowledge`, subject to user approval.",
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
        "- Save durable lessons and conventions with `propose_knowledge`, subject to user approval.",
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


def render_plan(plan: dict) -> str:
    marks = {"completed": "[x]", "in_progress": "[~]", "pending": "[ ]"}
    rows = [f"{marks.get(s['status'], '[ ]')} {s['step']}" + (f" — {s['owner']}" if s.get("owner") else "")
            for s in plan.get("steps", [])]
    return "\n".join(rows) + (f"\nNote: {plan['explanation']}" if plan.get("explanation") else "")


def render_event(event: dict, names: dict[str, str]) -> str:
    kind = event["kind"]
    who = lambda agent: f"{names.get(agent, agent)} ({agent})"  # noqa: E731
    if kind == "room_reply":
        done = "; ".join(a["label"] for a in event.get("actions") or [])
        return f"**{who(event['agent'])}:** {_clip(event['text'], 4500)}" + (f"\n(Actions: {done})" if done else "")
    if kind == "approval_requested":
        return ("Confirmation card " + event["approval_id"] + " waiting for the requester: "
                + "; ".join(event.get("items", [])))
    if kind == "approval_resolved":
        return f"Confirmation {event['approval_id']} {event['decision']}: {_clip(event.get('text', ''), 1500)}"
    if kind == "assistant_group_created":
        return f"Studio created {event['group_name']} ({event['target_group_id']})."
    if kind == "dispatch_sent":
        return f"Studio dispatched {event['target_task_id']} to {event['group_name']} ({event['target_group_id']})."
    if kind == "dispatch_finished":
        paths = ", ".join(c["path"] for c in event.get("artifacts", [])[:25])
        return (f"{event['group_name']} returned {event['target_task_id']}: {event['status']}. "
                f"{_clip(event.get('text') or event.get('error'), 1800)} Files: {paths}")
    if kind == "progress":
        return f"**{who(event['agent'])} (progress):** {_clip(event['text'], 1200)}"
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
    if kind == "plan":
        return f"**{who(event['agent'])} updated the plan:**\n" + render_plan(event)
    if kind == "answer":
        return f"**Requester answered:** {_clip(event['text'], 3000)}"
    if kind == "final":
        return f"**{who(event['agent'])} (final answer, task {event['status']}):** {_clip(event['text'], 5000)}"
    if kind == "task_finished":
        return f"— task finished: {event['status']} —"
    return ""


def conversation(store: GroupStore, before_seq: int, budget_chars: int = 48_000, *, after_seq: int = 0) -> str:
    names = {m.id: m.name for m in store.group.members}
    lines: list[str] = []
    used = 0
    for event in reversed(store.chat()):
        if event["seq"] >= before_seq:
            continue
        if event["seq"] <= after_seq:
            break
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
    if store.group.folder:
        sections.append("## Selected workspace\n"
                        f"User-selected folder: {store.group.folder}\n"
                        "Your current working directory (.) is a private checkout of this session's project, "
                        "including eligible files imported from that folder at task start. Use relative paths "
                        "with list_dir, find_files and read_file to inspect existing files. Published edits are "
                        "synced back to the selected folder when the task finishes.")
    sections.append("## Team project files\n" + listing(store.project.files()))
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
        self.studio = None
        self.dispatched_by = None
        self.continued_from = None
        self.initial_calls = []
        self.options = {}
        self.created_at = now()
        self.group = store.group
        self.interactive = self.group.mode == "interactive"
        self.delegations = 0
        self.assignments: list[dict] = []
        self.history: dict[str, list[str]] = {}
        self.lead_baseline: dict[str, str] = {}
        self.sessions: dict[tuple[str, str], AgentSession] = {}
        """Member conversations kept for this task, continued by later assignments."""
        self.session_marks: dict[tuple[str, str], dict] = {}
        """Per member session: the project manifest after its last publish and the seq of its last assignment."""
        self.lock = threading.RLock()
        """Guards counters, sessions and the record while assignments run in parallel."""
        self.plan: dict | None = None
        self.reviewer_provider: Provider | None = None
        self.parallel_batches = 0
        self.active: list[str] = []
        self.record: dict[str, Any] = {}
        self.control = Continuation()
        self.inflight: dict[str, dict] = {}
        """Assignments started but not finished, by assignee; a resumed task rejoins them."""
        self.loop_state: dict | None = None
        """Progress of the review loop in flight, so a resumed task continues the same round."""
        self.started_at = None

    def continuation_state(self):
        return {"task_id":self.task_id, "elapsed":time.monotonic()-self.started_at, "frames":self.control.frames, "team":self.group.to_dict(),
                "lead_baseline":self.lead_baseline, "inflight":self.inflight, "loop_state":self.loop_state,
                "sessions":{f"{member}|{kind}": {"messages":session.messages, "runs":session.runs,
                                                 "context":asdict(session.context) if session.context else None}
                            for (member, kind), session in self.sessions.items()},
                "session_marks":{f"{member}|{kind}": mark for (member, kind), mark in self.session_marks.items()},
                "plan":self.plan, "parallel_batches":self.parallel_batches,
                "delegations":self.delegations, "history":self.history, "assignments":self.assignments,
                "channel":self.channel.snapshot()}


    # -- plumbing ---------------------------------------------------------
    def _event(self, event: dict) -> None:
        event = {"task_id": self.task_id, **event}
        if event["type"] == "end" and event.get("agent") == self.primary:
            self.channel.finish()
        if event["type"] == "say":
            self.store.append("progress", task_id=self.task_id, agent=event["agent"], text=event["text"])
        if event["type"] == "parallel":
            with self.lock:
                self.parallel_batches += 1
        if event["type"] == "delta":
            self.store.previews.fragment(self.task_id, event["agent"], event["phase"], event["text"])
        elif event["type"] in ("step", "tool_call"):
            self.store.previews.fragment(self.task_id, event["agent"], "discard", "")
        if event["type"] != "delta":
            self.store.trace(self.task_id, event)
        if self.on_activity:
            self.on_activity(event)

    def _registry(self, agent_id: str, *, lead: bool) -> ToolRegistry:
        registry = build_default_registry()
        if self.continued_from:
            draft = recovery.draft_tool(self.store, self.continued_from, agent_id)
            if draft:
                registry.add(draft)
        if lead and len(self.group.members) > 1:
            registry.add(self._delegate_tool(agent_id))
            if self.settings.team.review_loops and len(self.group.members) > 2:
                registry.add(self._review_tool(agent_id))
            if self.settings.team.plans:
                registry.add(self._plan_tool(agent_id))
        if self.interactive:
            registry.add(Tool(
                name="ask_human",
                description=("Ask the human requester a question in the group chat and wait for the answer. "
                             "Use it for decisions only the requester can make; put all related questions in one message."),
                parameters={"type": "object", "properties": {
                    "question": {"type": "string", "description": "The complete question(s) for the requester"}},
                    "required": ["question"]},
                fn=lambda _ctx, args, agent=agent_id: self._ask(agent, args["question"])))
        if self.studio is not None:
            from .studio_tools import role_tools, assistant_tools
            extras = role_tools(self.studio, self.group, agent_id)
            extras.extend(self.studio.uploads.tools(self.store, agent_id))
            if self.group.kind == "assistant":
                extras.extend(assistant_tools(self.studio, self.group.id, self.task_id))
            for tool in extras:
                registry.add(tool)
        return registry

    def _ask(self, agent: str, question: str) -> ToolResult:
        question = question.strip()
        if not question:
            return ToolResult.failure("The question is empty.")
        if self.channel.pending() is None:
            self._event({"type": "question", "agent": agent, "question": question})
        try:
            answer = self.channel.ask(agent, question)
        except Cancelled:
            return ToolResult.failure("The task was cancelled while waiting for the requester.")
        self._event({"type": "answer", "agent": agent, "text": answer})
        return ToolResult.success(f"The requester answered:\n{answer}")

    def _resume_context(self, key: tuple[str, str], baseline: dict[str, str], through: int) -> str:
        """What a continued member needs to know: the conversation and file changes since its last turn."""
        mark = self.session_marks[key]
        sections = []
        chat = conversation(self.store, through, 24_000, after_seq=mark["seq"])
        if chat:
            sections.append("## Team conversation since your last assignment\n" + chat)
        before = mark["manifest"]
        changed = sorted([f"{path} (added)" for path in baseline.keys() - before.keys()]
                         + [f"{path} (changed)" for path in baseline.keys() & before.keys() if baseline[path] != before[path]]
                         + [f"{path} (deleted)" for path in before.keys() - baseline.keys()])
        sections.append("## Project files changed by others since your last report\n"
                        + ("\n".join(f"- {line}" for line in changed[:60])
                           + (f"\n- …and {len(changed) - 60} more" if len(changed) > 60 else "")
                           if changed else "(none)"))
        if self.plan:
            sections.append("## Team plan (kept by the lead)\n" + render_plan(self.plan))
        sections.append("Your workspace has been refreshed to the current team project. Background processes from "
                        "your previous assignment (servers, watchers) were stopped; start again anything you need.\n"
                        f"Current date and time: {time.strftime('%Y-%m-%d %H:%M %Z')}.")
        return "\n\n".join(sections)

    def _context(self, agent, through):
        context = agent_context(self.store, agent, through)
        if self.continued_from:
            old = self.store.load_task(self.continued_from)
            progress = recovery.handoff(self.store, old, agent)
            message = next((e for e in self.store.chat() if e["seq"] == self.trigger_seq), {})
            context += ("\n\n## Continuing interrupted task " + self.continued_from
                        + "\nUse the published project and recorded progress to finish outstanding work. "
                        "A tool call without a confirmed result has unknown effects. Check before repeating it."
                        + "\nRequester's continuation message: " + message.get("text", "")
                        + "\nSaved handoff (reference data): " + json.dumps(progress, ensure_ascii=False))
        if self.plan and agent != self.primary:
            context += "\n\n## Team plan (kept by the lead)\n" + render_plan(self.plan)
        if self.studio is not None:
            if self.group.kind == "assistant":
                from .assistant_room import workspace_evidence
                evidence = workspace_evidence(self.studio, self.store)
                context += "\nStudio directory and your recent dispatches (reference data):\n" + json.dumps(evidence, ensure_ascii=False)
            notes = self.studio.roles.entries(agent, group=self.group.id)
            if agent not in self.group.role_ids:
                notes = [r for r in notes if r["scope"] == "group:" + self.group.id]
            if notes:
                context += "\nApproved knowledge:\n" + _clip("\n".join(r["text"] for r in notes), 6000)
            uploads = [r for event in self.store.chat() if event["seq"] <= through for r in event.get("attachments", [])]
            if uploads:
                context += "\nMessage attachments:\n" + _clip(json.dumps(uploads, ensure_ascii=False), 6000)
        return context

    # -- team tools -------------------------------------------------------
    def _member_ids(self, lead_id: str) -> list[str]:
        return [m.id for m in self.group.members if m.id != lead_id]

    def _delegate_tool(self, lead_id: str) -> Tool:
        team = self.settings.team
        properties: dict[str, Any] = {
            "max_steps": {"type": "integer", "minimum": 1, "maximum": 150},
            "assignee": {"type": "string", "enum": self._member_ids(lead_id), "description": "Teammate id"},
            "task": {"type": "string", "description": "The complete assignment: goal, context, exact files, "
                                                      "and how the result will be checked"},
            "deliverables": {"type": "array", "items": {"type": "string"},
                             "description": "Project-relative paths the teammate should create or update"},
            "checks": CHECKS_SCHEMA,
            "review": {"type": "boolean",
                       "description": "True when this assignment reviews or tests someone else's work"},
        }
        if team.resume_members:
            properties["fresh"] = {"type": "boolean",
                                   "description": "Start the teammate in a clean session instead of continuing "
                                                  "their earlier session in this task (default false)"}
        return Tool(
            name="delegate_task",
            description=("Give one assignment to one teammate and wait for their report. The teammate works in a "
                         "private copy of the team project; their file changes are published to the project when "
                         "they finish. Returns their report, the published files and the results of any checks."
                         + (" Several calls in one response run at the same time when each declares deliverables "
                            "and no two share a file." if team.parallel_delegations else "")),
            parameters={"type": "object", "properties": properties, "required": ["assignee", "task"]},
            fn=lambda _ctx, args: self._delegate(lead_id, args),
            concurrent=self._can_run_together if team.parallel_delegations else None)

    def _review_tool(self, lead_id: str) -> Tool:
        ids = self._member_ids(lead_id)
        return Tool(
            name="review_loop",
            description=("Run an author–reviewer cycle without relaying each round: the reviewer reviews, and while "
                         "they request changes their findings go straight to the author's continued session to fix, "
                         "then back to the reviewer — until the reviewer approves or the rounds run out. Each review "
                         "and each fix counts as one delegation. Returns the final verdict, the last review, the "
                         "author's last report and the files changed."),
            parameters={"type": "object", "properties": {
                "author": {"type": "string", "enum": ids, "description": "Teammate who produced and will fix the work"},
                "reviewer": {"type": "string", "enum": ids, "description": "Teammate who reviews or tests it"},
                "work": {"type": "string", "description": "What was produced and where, and what the reviewer must check"},
                "criteria": {"type": "string", "description": "What 'acceptable' means for the reviewer's verdict"},
                "deliverables": {"type": "array", "items": {"type": "string"},
                                 "description": "Project-relative paths the author may change while fixing"},
                "checks": CHECKS_SCHEMA,
                "max_rounds": {"type": "integer", "minimum": 1, "maximum": 5,
                               "description": "Most review rounds (default 3)"},
            }, "required": ["author", "reviewer", "work"]},
            fn=lambda _ctx, args: self._review_loop(lead_id, args))

    def _plan_tool(self, lead_id: str) -> Tool:
        return Tool(
            name="update_plan",
            description=("Replace the task plan the requester sees. List every step with its status; update it when "
                         "you start or finish a step or change the approach."),
            parameters={"type": "object", "properties": {
                "explanation": {"type": "string", "description": "Optional short note on what changed"},
                "steps": {"type": "array", "minItems": 1, "maxItems": 20, "items": {
                    "type": "object", "properties": {
                        "step": {"type": "string"},
                        "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                        "owner": {"type": "string", "description": "Teammate id responsible, if any"}},
                    "required": ["step", "status"]}},
            }, "required": ["steps"]},
            fn=lambda _ctx, args: self._update_plan(lead_id, args))

    # -- delegation ---------------------------------------------------------
    def _can_run_together(self, batch: list[dict]) -> bool:
        """A batch of delegations may run at once: distinct teammates, declared and disjoint files, budget left."""
        assignees = [str(args.get("assignee", "")).strip() for args in batch]
        if len(set(assignees)) != len(assignees) or any(args.get("review") for args in batch):
            return False
        claimed: list[list[str]] = []
        for args in batch:
            paths = [_norm(p) for p in args.get("deliverables") or [] if _norm(p)]
            if not paths:
                return False
            claimed.append(paths)
        for i, mine in enumerate(claimed):
            for theirs in claimed[i + 1:]:
                if any(_overlaps(a, b) for a in mine for b in theirs):
                    return False
        with self.lock:
            new = sum(1 for name in assignees if name not in self.inflight)
            return self.settings.team.max_delegations - self.delegations >= new

    def _delegate(self, lead_id: str, args: dict) -> ToolResult:
        assignee = str(args.get("assignee", "")).strip()
        task = str(args.get("task", "")).strip()
        deliverables = [str(p).strip() for p in args.get("deliverables") or [] if str(p).strip()][:40]
        if len(task) < 10:
            return ToolResult.failure("The assignment is empty or too short. Describe the work completely.")
        fresh, review = args.get("fresh", False), args.get("review", False)
        if not isinstance(fresh, bool) or not isinstance(review, bool):
            return ToolResult.failure("fresh and review must be true or false")
        try:
            checks = validate(list(args.get("checks") or []))
        except ValueError as exc:
            return ToolResult.failure(f"Invalid checks: {exc}")
        outcome = self._assign(lead_id, assignee, task, deliverables=deliverables, max_steps=args.get("max_steps"),
                               fresh=fresh, checks=checks, review=review)
        if isinstance(outcome, ToolResult):
            return outcome
        lines = self._report_lines(outcome)
        lines.append(f"Delegations used: {self.delegations}/{self.settings.team.max_delegations}.")
        ok = outcome["result"].status in {"completed", "max_steps", "stuck", "budget"}
        return ToolResult(ok=ok, content="\n".join(lines))

    def _assign(self, lead_id: str, assignee: str, task: str, *, deliverables: list[str], max_steps=None,
                fresh: bool = False, checks: list[dict] | None = None, review: bool = False,
                loop: dict | None = None) -> dict | ToolResult:
        """Run one member assignment; returns its outcome, or a refusal for the lead."""
        valid = self._member_ids(lead_id)
        if assignee not in valid:
            return ToolResult.failure(f"Unknown assignee {assignee!r}. Choose one of: {', '.join(valid)}.")
        with self.lock:
            entry = self.inflight.get(assignee)
        if entry is None:
            entry = self._open_assignment(lead_id, assignee, task, deliverables, max_steps, checks, review, loop)
            if isinstance(entry, ToolResult):
                return entry
        member = self.group.member(assignee)
        result, changes, skipped = self._run_member(member, task, deliverables, entry, max_steps,
                                                    fresh=fresh, review=review)
        with self.lock:
            self.inflight.pop(assignee, None)
            self.lead_baseline = self.store.project.checkout(self.store.workspace(lead_id))
        return self._close_assignment(member, task, deliverables, result, changes, skipped, checks, review, loop)

    def _open_assignment(self, lead_id, assignee, task, deliverables, max_steps, checks, review, loop):
        """Budget, repeat check, lead publish and the delegation event; returns the in-flight entry."""
        if max_steps is not None and (type(max_steps) is not int or not 1 <= max_steps <= min(150, self.settings.limits.max_steps)):
            return ToolResult.failure(f"max_steps must be between 1 and {min(150, self.settings.limits.max_steps)}")
        with self.lock:
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
            lead_workspace = self.store.workspace(lead_id)
            own, _ = self.store.project.publish(lead_workspace, self.lead_baseline, author=lead_id,
                                                task_id=self.task_id, note="lead edits before delegation")
            if own:
                self.store.append("publish", task_id=self.task_id, agent=lead_id, changes=[c.as_dict() for c in own])
                self.lead_baseline = self.store.project.checkout(lead_workspace)
        extra = {k: v for k, v in (("review", review or None), ("loop", loop), ("checks", checks or None)) if v}
        event = self.store.append("delegation", task_id=self.task_id, agent=lead_id, assignee=assignee,
                                  task=task, deliverables=deliverables, **extra)
        entry = {"assignee": assignee, "seq": event["seq"]}
        with self.lock:
            self.inflight[assignee] = entry
        return entry

    def _close_assignment(self, member, task, deliverables, result, changes, skipped, checks, review, loop):
        assignee = member.id
        report = result.answer or "(no report)"
        if result.error:
            report += f"\n\n[Run problem: {result.error}]"
        checked = evaluate(self.store, checks, self.settings) if checks else None
        if checked is not None:
            with self.lock:
                for item in reversed(self.assignments):
                    if item["agent"] == assignee and "checks" not in item:
                        item["checks"] = {"passed": checked["passed"], "total": checked["total"]}
                        break
        manifest = self.store.project.manifest()
        missing = [p for p in deliverables if not _present(p, manifest)]
        report_extra = {k: v for k, v in (("checks", checked), ("loop", loop), ("review", review or None)) if v}
        self.store.append("report", task_id=self.task_id, agent=assignee, status=result.status, text=report,
                          changes=[c.as_dict() for c in changes], steps=result.steps, tool_calls=result.tool_calls,
                          **report_extra)
        return {"member": member, "result": result, "report": report, "changes": changes, "skipped": skipped,
                "missing": missing, "checks": checked}

    def _report_lines(self, outcome: dict) -> list[str]:
        member, result, changes = outcome["member"], outcome["result"], outcome["changes"]
        lines = [f"Report from {member.name} ({member.id}) — status: {result.status}"
                 + ("" if result.status == "completed" else " (the assignment did not finish normally)"),
                 "", _clip(outcome["report"], REPORT_LIMIT), ""]
        if self.settings.team.resume_members:
            lines.append(f"Session: {'continued their earlier session in this task' if result.resumed else 'new session'}.")
        if changes:
            lines.append("Published to the team project: " + ", ".join(
                f"{c.path} ({c.change}{', overwrote a concurrent change' if c.conflict else ''})" for c in changes))
        else:
            lines.append("No files were changed by this assignment.")
        if outcome["missing"]:
            lines.append("Expected files that do not exist in the project: " + ", ".join(outcome["missing"]))
        if outcome["skipped"]:
            lines.append("Not published (skipped): " + ", ".join(outcome["skipped"][:20]))
        if outcome["checks"]:
            lines += check_lines(outcome["checks"])
        return lines

    def _review_loop(self, lead_id: str, args: dict) -> ToolResult:
        author, reviewer = str(args.get("author", "")).strip(), str(args.get("reviewer", "")).strip()
        work, criteria = str(args.get("work", "")).strip(), str(args.get("criteria", "") or "").strip()
        deliverables = [str(p).strip() for p in args.get("deliverables") or [] if str(p).strip()][:40]
        rounds_max = args.get("max_rounds", 3)
        valid = self._member_ids(lead_id)
        if author not in valid or reviewer not in valid:
            return ToolResult.failure(f"author and reviewer must be teammates: {', '.join(valid)}")
        if author == reviewer:
            return ToolResult.failure("The reviewer must be someone other than the author.")
        if len(work) < 10:
            return ToolResult.failure("Describe the work to review and where it is.")
        if type(rounds_max) is not int or not 1 <= rounds_max <= 5:
            return ToolResult.failure("max_rounds must be between 1 and 5")
        try:
            checks = validate(list(args.get("checks") or []))
        except ValueError as exc:
            return ToolResult.failure(f"Invalid checks: {exc}")
        names = {m.id: m.name for m in self.group.members}
        key = f"{author}>{reviewer}"
        state = self.loop_state if self.loop_state and self.loop_state.get("key") == key else None
        if state is None:  # a resumed task continues the saved round and phase instead
            state = self.loop_state = {"key": key, "id": uuid.uuid4().hex[:8], "number": 1, "phase": "review",
                                       "rounds": [], "verdict": "no review", "last_review": "", "findings": "",
                                       "last_fix": "", "last_checks": None, "touched": {}, "stop": ""}
        try:
            while state["number"] <= rounds_max:
                number = state["number"]
                if state["phase"] == "review":
                    review_task = (f"Review round {number} of at most {rounds_max} in a review loop with "
                                   f"{names[author]} ({author}), who will fix what you find.\n\n{work}"
                                   + (f"\n\nAcceptance criteria:\n{criteria}" if criteria else "")
                                   + ("\n\n" + "\n".join(check_lines(state["last_checks"])) if state["last_checks"] else "")
                                   + "\n\nInspect the actual files and run what is needed. Do not fix the work yourself. "
                                     "Begin your report with exactly one line: `VERDICT: APPROVED` if the work meets the "
                                     "criteria, or `VERDICT: CHANGES REQUESTED` followed by numbered, concrete findings the "
                                     "author can act on.")
                    reviewed = self._assign(lead_id, reviewer, review_task, deliverables=[], review=True,
                                            loop={"id": state["id"], "round": number, "role": "review"})
                    if isinstance(reviewed, ToolResult):
                        state["stop"] = reviewed.content
                        break
                    state["last_review"] = reviewed["report"]
                    state["verdict"] = _verdict(reviewed["report"])
                    state["rounds"].append(f"Round {number} — review by {names[reviewer]}: {state['verdict'].upper()}"
                                           + (f" ({reviewed['result'].status})" if reviewed["result"].status != "completed" else ""))
                    if (state["verdict"] == "approved" or reviewed["result"].status in ("error", "cancelled")
                            or number == rounds_max):
                        break
                    state["findings"] = VERDICT_LINE.sub("", reviewed["report"], count=1).strip() or reviewed["report"]
                    state["phase"] = "fix"
                fix_task = (f"Review round {number}: {names[reviewer]} requested changes. Fix these findings in the "
                            f"project, verify your fix, then report what you changed and how you verified it.\n\n"
                            f"{state['findings']}")
                fixed = self._assign(lead_id, author, fix_task, deliverables=deliverables, checks=checks,
                                     loop={"id": state["id"], "round": number, "role": "fix"})
                if isinstance(fixed, ToolResult):
                    state["stop"] = fixed.content
                    break
                state["last_fix"], state["last_checks"] = fixed["report"], fixed["checks"]
                for change in fixed["changes"]:
                    state["touched"][change.path] = change.change
                state["rounds"][-1] += (f"; fix by {names[author]}: {fixed['result'].status}, "
                                        f"{len(fixed['changes'])} file(s) changed")
                if fixed["result"].status in ("error", "cancelled"):
                    break
                state["phase"], state["number"] = "review", number + 1
        except PauseRequested:
            raise  # the saved loop state lets the resumed task continue this round
        except Exception:
            self.loop_state = None
            raise
        self.loop_state = None
        rounds, verdict, stop = state["rounds"], state["verdict"], state["stop"]
        last_review, last_fix, last_checks, touched = (state["last_review"], state["last_fix"],
                                                       state["last_checks"], state["touched"])
        lines = [f"Review loop {names[author]} ⇄ {names[reviewer]}: {verdict.upper()} after {len(rounds)} review round(s).",
                 *rounds]
        if stop:
            lines.append("Stopped early: " + stop)
        if last_review:
            lines += ["", f"Last review from {names[reviewer]}:", _clip(last_review, 6000)]
        if last_fix:
            lines += ["", f"Last report from {names[author]}:", _clip(last_fix, 3000)]
        if touched:
            lines.append("Files changed by the fixes: " + ", ".join(f"{p} ({c})" for p, c in sorted(touched.items())))
        if last_checks:
            lines += check_lines(last_checks)
        lines.append(f"Delegations used: {self.delegations}/{self.settings.team.max_delegations}.")
        return ToolResult(ok=bool(rounds), content="\n".join(lines))

    def _update_plan(self, lead_id: str, args: dict) -> ToolResult:
        steps = []
        for item in args.get("steps") or []:
            text = str(item.get("step", "")).strip()
            if not 1 <= len(text) <= 300:
                return ToolResult.failure("Each step needs 1–300 characters of text.")
            row = {"step": text, "status": item.get("status", "pending")}
            if item.get("owner"):
                row["owner"] = str(item["owner"]).strip()[:60]
            steps.append(row)
        if not 1 <= len(steps) <= 20:
            return ToolResult.failure("A plan has 1–20 steps.")
        explanation = str(args.get("explanation") or "").strip()[:600]
        with self.lock:
            self.plan = {"steps": steps, "explanation": explanation, "updated_at": now()}
            self._update(plan=self.plan)
        self.store.append("plan", task_id=self.task_id, agent=lead_id, steps=steps, explanation=explanation)
        done = sum(s["status"] == "completed" for s in steps)
        return ToolResult(ok=True, content=f"Plan updated: {done}/{len(steps)} steps done. The requester can see it.")

    def _run_member(self, member: Member, task: str, deliverables: list[str], entry: dict, max_steps=None,
                    *, fresh: bool = False, review: bool = False):
        trigger_seq = entry["seq"]
        workspace = self.store.workspace(member.id)
        if "baseline" not in entry:
            entry["baseline"] = self.store.project.checkout(workspace)
        baseline = entry["baseline"]
        rejoining = member.id in self.control.frames
        # Review work may use another model or thinking mode, so it keeps a session of its own.
        key = (member.id, "review" if review else "work")
        session = None
        if self.settings.team.resume_members:
            with self.lock:
                session = self.sessions.get(key)
                if session is None or (fresh and not rejoining):
                    session = self.sessions[key] = AgentSession()
        continuing = (not rejoining and session is not None and session.resumable and key in self.session_marks)
        lead = self.group.member(self.primary)
        assignment = task
        if deliverables:
            assignment += "\n\nFiles you are expected to create or update: " + ", ".join(deliverables)
        assignment = f"Assignment from {lead.name} ({lead.id}):\n\n" + assignment
        settings = self.settings.reviewer() if review else self.settings
        provider = self.reviewer_provider if review and self.reviewer_provider is not None else self.provider
        agent = Agent(member.id, provider, self._registry(member.id, lead=False), Sandbox(workspace, member.id),
                      settings, limits=settings.limits if max_steps is None else replace(settings.limits, max_steps=max_steps),
                      system_prompt=member_prompt(member, lead, self.group.members, settings, self.interactive),
                      on_event=self._event, should_cancel=self.should_cancel, inbox=self.channel,
                      stream=True, continuation=self.control)
        if not rejoining:
            self.channel.start_cursor(member.id, trigger_seq)
        with self.lock:
            self.active.append(member.id)
            self._update(active=list(self.active))
        context = ("" if rejoining else self._resume_context(key, baseline, trigger_seq) if continuing
                   else self._context(member.id, trigger_seq))
        try:
            result = agent.run(assignment, context=context, session=session)
        finally:
            with self.lock:
                self.active.remove(member.id)
                self._update(active=list(self.active))
        changes, skipped = self.store.project.publish(workspace, baseline, author=member.id, task_id=self.task_id,
                                                      note=task[:300])
        with self.lock:
            if session is not None:
                self.session_marks[key] = {"manifest": self.store.project.manifest(), "seq": trigger_seq}
            metrics = _metrics(member.id, result)
            if review:
                metrics["review"] = True
            self.assignments.append(metrics)
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
        self.started_at = time.monotonic() - self.control.saved.get("elapsed",0)
        me = self.group.member(self.primary)
        can_delegate = self.primary == self.group.lead and len(self.group.members) > 1
        workspace = self.store.workspace(self.primary)
        saved = self.control.saved
        if saved:
            self.record = self.store.load_task(self.task_id)
            self.lead_baseline = saved["lead_baseline"]
            self.delegations = saved["delegations"]
            legacy = saved.get("delegation")
            self.inflight = saved.get("inflight") or ({legacy["assignee"]: legacy} if legacy else {})
            self.loop_state = saved.get("loop_state")
            self.plan = saved.get("plan")
            self.parallel_batches = saved.get("parallel_batches", 0)
            for name, data in (saved.get("sessions") or {}).items():
                member, kind = name.split("|", 1)
                self.sessions[(member, kind)] = AgentSession(
                    messages=data["messages"], runs=data["runs"],
                    context=ContextManager(**data["context"]) if data.get("context") else None)
            self.session_marks = {tuple(name.split("|", 1)): mark
                                  for name, mark in (saved.get("session_marks") or {}).items()}
            self.history, self.assignments = saved["history"], saved["assignments"]
            self._update(status="running", active=[self.primary])
        else:
            self.record = {"id": self.task_id, "group_id": self.group.id, "text": self.text, "primary": self.primary,
                           "status": "running", "created_at": self.created_at, "trigger_seq": self.trigger_seq, "finished_at": None, "active": [self.primary],
                           "delegations": 0, "assignments": [], "final_answer": "", "error": "", "options": self.options, "role_ids": list(self.group.role_ids)}
            if getattr(self, "redispatched_from", None):
                self.record["redispatched_from"] = self.redispatched_from
            if self.continued_from:
                self.record["continued_from"] = self.continued_from
                self.plan = self.store.load_task(self.continued_from).get("plan")
                if self.plan:
                    self.record["plan"] = self.plan
            self.record["room_kind"] = self.group.kind
            if self.dispatched_by:
                self.record["dispatched_by"] = self.dispatched_by
            self.store.save_task(self.record)
            self.store.append("task_started", task_id=self.task_id, agent=self.primary,
                              **({"continued_from":self.continued_from} if self.continued_from else {}))
            self.lead_baseline = self.store.project.checkout(workspace)
        settings = self.settings
        # The coordinating lead gets the lead budget; a member addressed directly works
        # like any assignment, with the member budget.
        limits = replace(settings.limits, max_steps=settings.team.lead_max_steps) if can_delegate else settings.limits
        prompt = lead_prompt(me, self.group.members, settings, self.interactive, can_delegate,
                             studio_assistant=self.group.kind == "assistant")
        if not can_delegate and self.primary != self.group.lead:
            lead = self.group.member(self.group.lead)
            prompt += (f"\n\nThe requester addressed this message to you directly, so you handle it yourself. "
                       f"The team lead is {lead.name} ({lead.id}).")
        agent = Agent(self.primary, self.provider, self._registry(self.primary, lead=can_delegate),
                      Sandbox(workspace, self.primary), settings, system_prompt=prompt, limits=limits,
                      on_event=self._event, should_cancel=self.should_cancel, inbox=self.channel, stream=True, continuation=self.control)
        if not saved:
            self.channel.start_cursor(self.primary, self.trigger_seq)
        assignment = self.text
        if self.continued_from:
            assignment = ("Continue the interrupted task from the saved handoff and current project. "
                          "Carry out the outstanding work. Original assignment:\n" + self.text)
        result = agent.run(assignment, context=self._context(self.primary, self.trigger_seq), initial_calls=self.initial_calls)
        changes, _ = self.store.project.publish(workspace, self.lead_baseline, author=self.primary,
                                                task_id=self.task_id, note="final edits")
        if changes:
            self.store.append("publish", task_id=self.task_id, agent=self.primary,
                              changes=[c.as_dict() for c in changes])
        status = {"completed": "completed", "dispatched":"dispatched", "cancelled": "cancelled", "error": "failed"}.get(result.status, "incomplete")
        answer = result.answer or ("" if status in ("cancelled","dispatched") else "(The lead stopped without a final answer.)")
        if answer:
            self.store.append("final", task_id=self.task_id, agent=self.primary, text=answer, status=status,
                              error=result.error)
        totals = _metrics(self.primary, result)
        usage = Usage(prompt_tokens=totals["prompt_tokens"], completion_tokens=totals["completion_tokens"])
        for item in self.assignments:
            usage = usage.merged(Usage(prompt_tokens=item["prompt_tokens"], completion_tokens=item["completion_tokens"]))
        metrics = {"duration_s": round(time.monotonic() - self.started_at, 1), "lead": totals,
                   "delegations": self.delegations, "questions": self.channel.questions,
                   "prompt_tokens": usage.prompt_tokens, "completion_tokens": usage.completion_tokens,
                   "prompt_cache_hit_tokens": totals["prompt_cache_hit_tokens"]
                   + sum(a.get("prompt_cache_hit_tokens", 0) for a in self.assignments),
                   "resumed_assignments": sum(1 for a in self.assignments if a.get("resumed")),
                   "review_assignments": sum(1 for a in self.assignments if a.get("review")),
                   "parallel_batches": self.parallel_batches,
                   "steps": totals["steps"] + sum(a["steps"] for a in self.assignments),
                   "tool_calls": totals["tool_calls"] + sum(a["tool_calls"] for a in self.assignments)}
        self._update(status=status, finished_at=now(), final_answer=answer, error=result.error,
                     active=[], metrics=metrics, assignments=self.assignments, delegations=self.delegations)
        self.store.append("task_finished", task_id=self.task_id, status=status, metrics=metrics, error=result.error)
        return self.record


def _metrics(agent: str, result: AgentResult) -> dict:
    return {"agent": agent, "status": result.status, "steps": result.steps, "tool_calls": result.tool_calls,
            "prompt_tokens": result.usage.prompt_tokens, "completion_tokens": result.usage.completion_tokens,
            "prompt_cache_hit_tokens": result.usage.prompt_cache_hit_tokens or 0,
            "duration_s": result.duration_s, "compactions": result.compactions, "error": result.error,
            "resumed": result.resumed}
