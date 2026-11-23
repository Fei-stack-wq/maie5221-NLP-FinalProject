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

from .agent import FAILED_REQUEST_NOTE, Agent, AgentResult, AgentSession
from .checkpoint import Continuation, PauseRequested
from .config import FAST_SOFT_MINUTES, Settings
from .context import ContextManager
from .evaluation import evaluate, validate
from .meter import TaskMeter
from .providers import Provider
from .sandbox import Sandbox
from . import recovery
from .schemas import ToolResult, Usage
from .store import GroupStore, Member, now
from .tools import Tool, ToolRegistry, build_default_registry, validate_arguments
from .workspace import Change, listing

REPORT_LIMIT = 14_000
MODEL_ERROR_RETRIES = 1
"""After a failed model request, a member continues its own session this many times before reporting."""
MODEL_ERROR_PAUSE = 5.0
"""Seconds to wait before continuing after a failed model request."""
RECONCILE_STEPS = 30
"""Step budget for a teammate's check of its parallel teammates' results."""
BATCH_WAIT_SECONDS = 6 * 3600
"""Upper bound on waiting for the rest of a parallel batch (every exit path also releases the wait)."""


class ParallelBatch:
    """Delegations the lead started together, joined again before anyone reports (a deferred fan-in).

    Every member arrives exactly once, with its outcome, with nothing (refused before it ran), or by
    breaking the batch (a pause or an exception), so no member can wait for one that will never come.
    """

    def __init__(self, calls: set[tuple[str, str]]):
        self.calls = calls
        self.joined: set[str] = set()
        self.arrived: set[str] = set()
        self.outcomes: dict[str, dict] = {}
        self.broken = False
        self._cond = threading.Condition()

    def arrive(self, member: str, outcome: dict | None = None) -> None:
        with self._cond:
            if outcome is not None:
                self.outcomes[member] = outcome
            self.arrived.add(member)
            self._cond.notify_all()

    def abort(self) -> None:
        with self._cond:
            self.broken = True
            self._cond.notify_all()

    def wait(self, should_cancel: Callable[[], bool], timeout: float = BATCH_WAIT_SECONDS) -> bool:
        """True when every member arrived; False if the batch broke, the task was cancelled or time ran out."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while len(self.arrived) < len(self.calls) and not self.broken:
                if should_cancel() or time.monotonic() >= deadline:
                    return False
                self._cond.wait(0.5)
            return not self.broken

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


def _mentions(text: str, name: str) -> bool:
    """``name`` (a path or a teammate id) appears in ``text`` as a whole word or path, not inside another."""
    return re.search(r"(?<![\w./-])" + re.escape(name) + r"(?![\w-])", text) is not None


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
        self._notes: dict[str, list[str]] = {}
        self.questions = 0
        self.control = None

    def snapshot(self):
        with self._cond:
            return {"pending":self._pending, "answer":self._answer, "messages":list(self._messages),
                    "cursors":dict(self._cursors), "questions":self.questions,
                    "notes":{agent: list(items) for agent, items in self._notes.items()}}

    def restore(self, saved):
        self._pending, self._answer = saved["pending"], saved["answer"]
        self._messages, self._cursors, self.questions = saved["messages"], saved["cursors"], saved["questions"]
        self._notes = {agent: list(items) for agent, items in (saved.get("notes") or {}).items()}


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

    def note(self, agent: str, text: str) -> None:
        """A Studio notice for one agent (e.g. a soft budget mark), delivered at its next step."""
        with self._cond:
            self._notes.setdefault(agent, []).append(text)

    def take(self, agent: str) -> list[str]:
        with self._cond:
            cursor = self._cursors.get(agent, 0)
            new = self._messages[cursor:]
            self._cursors[agent] = len(self._messages)
            notes = self._notes.pop(agent, [])
        return notes + [f"[Message from the requester, sent while the team was working]\n{text}" for _, text in new]

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
            "- Verification has an owner. Rely on the evidence of whoever reviewed or tested the work and on Studio's "
            "check results instead of re-running their tests; spot-check yourself only when a report is vague, a "
            "check failed or something important was not covered. You may write files yourself when that is part "
            "of your role.",
            "- Size each assignment: pass a smaller `max_steps` for short pieces of work (notes, a summary, one "
            "fix) so a teammate does not wander.",
        ]
        if settings.team.parallel_delegations:
            parts.append(
                "- Assignments that need nothing from each other can run at the same time: call `delegate_task` "
                "several times in one response, each with `independent: true`, its own `deliverables` and the "
                "`inputs` it reads. When they finish, each teammate sees the others' results and fixes anything that "
                "turned out to depend on them. Good candidates: release notes, a user guide or a test plan drafted "
                "while the build or the tests run; separate research topics for separate researchers. Not "
                "candidates: anything that reviews, summarises, prices, presents or builds on work that does not "
                "exist yet — a risk review before the research, an outline before the reporting, client material "
                "before the analysis. Those go one after another.")
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
                "report (mark steps done, add or change steps). Give a step `after` when it needs earlier steps; "
                "steps whose `after` steps are done can be started together. The requester sees the plan and may "
                "steer you.")
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
             "chat automatically. Use await_result only when your own next step needs that result. Delegated work "
             "runs in standard mode; set `options.mode` to \"fast\" only when the requester explicitly asks for fast "
             "mode for that work (for example \"用快速模式\" or \"fast mode\"), never on your own judgement." if studio_assistant else
             "- Do the work yourself with your tools and verify it before you finish."),
        ]
    if settings.team.fast:
        parts += [
            "- FAST MODE: the requester wants a usable result within about 10–15 minutes and accepts a simpler "
            "one. The task stops at 15 minutes; Studio warns you at 5, 8 and 10. Choose the simplest approach that "
            "meets the request and decide details yourself. Do small pieces yourself instead of delegating; "
            "delegate only a few substantial pieces, in parallel when they are independent. Check the essential "
            "behaviour once, skip optional documents, polish and repeated reviews, and say in your final answer "
            "what was simplified or left out.",
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
        "- Work efficiently: put independent tool calls in one response — read several files, run several "
        "searches or fetches at once. Reads, searches and fetches in the same response run in parallel, and "
        "every extra response costs model time.",
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
        "- Verify in proportion: when you built something, run the fast checks for it (unit tests, an HTTP call, "
        "opening the file) before reporting, and leave full end-to-end walk-throughs to the teammate who reviews "
        "or tests it unless the assignment asks you for them. When you review or test, you own the verification: "
        "check the real behaviour and report concrete evidence.",
        "- Work efficiently: put independent tool calls in one response — read several files, run several "
        "searches or fetches at once. Reads, searches and fetches in the same response run in parallel, and "
        "every extra response costs model time.",
        "- Do not leave throwaway files in the project. Keep tests or evidence the team should keep in sensible "
        "places (e.g. tests/, docs/); delete scratch files before you finish.",
    ]
    if interactive:
        parts.append("- Use `ask_human` only for decisions that only the requester can make. Otherwise make a "
                     "reasonable assumption and state it in your report.")
    else:
        parts.append("- The requester is not available. Make reasonable assumptions and state them in your report.")
    if settings.team.fast:
        parts.append("- FAST MODE: deliver the essential result in as few steps as possible, check it once, add no "
                     "extra documents unless asked, and keep your report short.")
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
    steps = plan.get("steps", [])
    numbered = any(s.get("after") for s in steps)
    rows = [f"{marks.get(s['status'], '[ ]')} " + (f"{i}. " if numbered else "") + s["step"]
            + (f" — {s['owner']}" if s.get("owner") else "")
            + (f" (after {', '.join(map(str, s['after']))})" if s.get("after") else "")
            for i, s in enumerate(steps, 1)]
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
        self.member_provider: Provider | None = None
        """Fast mode: members' own work runs on a provider without thinking."""
        self.deadline: float | None = None
        self.parallel_batches = 0
        self.batch: ParallelBatch | None = None
        """The parallel batch whose assignments are about to start (see _can_run_together)."""
        self.parallel_reads = 0
        self.meter = TaskMeter(notify=lambda text: self.channel.note(self.primary, text))
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
                if event.get("tool") == "reads":
                    self.parallel_reads += 1
                else:
                    self.parallel_batches += 1
        if event["type"] != "delta":
            self.meter.record(event)
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
            "inputs": {"type": "array", "items": {"type": "string"}, "maxItems": 40,
                       "description": "Project-relative files or folders this assignment reads or builds on"},
        }
        if team.parallel_delegations:
            properties["independent"] = {
                "type": "boolean",
                "description": "True only if this assignment needs nothing from the other assignments in the same "
                               "response. Such assignments run at the same time; afterwards each teammate checks the "
                               "others' results and fixes what depended on them."}
        if team.resume_members:
            properties["fresh"] = {"type": "boolean",
                                   "description": "Start the teammate in a clean session instead of continuing "
                                                  "their earlier session in this task (default false)"}
        return Tool(
            name="delegate_task",
            description=("Give one assignment to one teammate and wait for their report. The teammate works in a "
                         "private copy of the team project; their file changes are published to the project when "
                         "they finish. Returns their report, the published files and the results of any checks."
                         + (" Several calls in one response run at the same time when each is marked independent, "
                            "declares deliverables and no two share a file." if team.parallel_delegations else "")),
            parameters=self._delegate_schema(properties),
            fn=lambda _ctx, args: self._delegate(lead_id, args),
            concurrent=self._can_run_together if team.parallel_delegations else None)

    def _delegate_schema(self, properties: dict) -> dict:
        self.delegate_parameters = {"type": "object", "properties": properties, "required": ["assignee", "task"]}
        return self.delegate_parameters

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
                        "owner": {"type": "string", "description": "Teammate id responsible, if any"},
                        "after": {"type": "array", "items": {"type": "integer", "minimum": 1},
                                  "description": "Numbers of earlier steps (1-based) this step needs first"}},
                    "required": ["step", "status"]}},
            }, "required": ["steps"]},
            fn=lambda _ctx, args: self._update_plan(lead_id, args))

    # -- delegation ---------------------------------------------------------
    def _can_run_together(self, batch: list[dict]) -> bool:
        """A batch of delegations may run at once: each marked independent and valid, distinct teammates,
        declared and disjoint files, no brief naming another's files or teammate, and budget left.

        The lead can still be wrong about independence; the follow-up in _reconcile covers that case.
        """
        assignees = [str(args.get("assignee", "")).strip() for args in batch]
        if len(set(assignees)) != len(assignees) or any(args.get("review") for args in batch):
            return False
        if not all(args.get("independent") is True for args in batch):
            return False
        schema = getattr(self, "delegate_parameters", None)
        if schema is None or any(validate_arguments(schema, args) for args in batch):
            return False  # a call that never starts could not release its teammates
        if any(len(str(args.get("task", "")).strip()) < 10 for args in batch):
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
        # Declared inputs make a dependency explicit: nothing may read what another call in the batch produces.
        for i, args in enumerate(batch):
            reads = [_norm(p) for p in args.get("inputs") or [] if _norm(p)]
            for j, theirs in enumerate(claimed):
                if i != j and any(_overlaps(a, b) for a in reads for b in theirs):
                    return False
        # A brief that names another assignment's files or teammate probably needs its output.
        for i, args in enumerate(batch):
            text = str(args.get("task", ""))
            for j, other in enumerate(batch):
                if i != j and any(_mentions(text, name) for name in [*claimed[j], assignees[j]]):
                    return False
        with self.lock:
            new = sum(1 for name in assignees if name not in self.inflight)
            if self.settings.team.max_delegations - self.delegations < new:
                return False
            # A batch resumed after a pause has lost its finished members; they are not joined again.
            if new == len(assignees):
                self.batch = ParallelBatch({(a, str(args.get("task", "")).strip()) for a, args in zip(assignees, batch)})
            return True

    def _join_batch(self, assignee: str, task: str) -> ParallelBatch | None:
        with self.lock:
            batch = self.batch
            if batch and (assignee, task) in batch.calls and assignee not in batch.joined:
                batch.joined.add(assignee)
                if len(batch.joined) == len(batch.calls):
                    self.batch = None
                return batch
        return None

    def _delegate(self, lead_id: str, args: dict) -> ToolResult:
        assignee = str(args.get("assignee", "")).strip()
        batch = self._join_batch(assignee, str(args.get("task", "")).strip())
        try:
            outcome = self._delegate_one(lead_id, args, batch)
        except BaseException:
            if batch is not None and assignee not in batch.arrived:
                batch.abort()  # paused or failed: the others report without the follow-up
            raise
        if batch is not None and assignee not in batch.arrived:
            batch.arrive(assignee)  # refused before it ran; the others still check each other's results
        return outcome

    def _delegate_one(self, lead_id: str, args: dict, batch: ParallelBatch | None) -> ToolResult:
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
        inputs = [str(p).strip() for p in args.get("inputs") or [] if str(p).strip()][:40]
        missing = []
        if inputs:
            manifest = self.store.project.manifest()
            missing = [p for p in inputs if not _present(p, manifest)]
            task += "\n\nRead these project files first: " + ", ".join(inputs)
            if missing:
                task += ("\nNot in the project yet: " + ", ".join(missing)
                         + ". Say in your report if the work needed them.")
        outcome = self._assign(lead_id, assignee, task, deliverables=deliverables, max_steps=args.get("max_steps"),
                               fresh=fresh, checks=checks, review=review, batch=batch)
        if isinstance(outcome, ToolResult):
            return outcome
        lines = self._report_lines(outcome)
        if missing:
            lines.append("Inputs that were not in the project when the assignment started: " + ", ".join(missing)
                         + ". If the work depended on them, run it again after the step that produces them.")
        lines.append(f"Delegations used: {self.delegations}/{self.settings.team.max_delegations}.")
        ok = outcome["result"].status in {"completed", "max_steps", "stuck", "budget"}
        return ToolResult(ok=ok, content="\n".join(lines))

    def _assign(self, lead_id: str, assignee: str, task: str, *, deliverables: list[str], max_steps=None,
                fresh: bool = False, checks: list[dict] | None = None, review: bool = False,
                loop: dict | None = None, batch: ParallelBatch | None = None) -> dict | ToolResult:
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
        if batch is not None:
            entry["peers"] = sorted(name for name, _ in batch.calls if name != assignee)
        parallel = None
        if "first" in entry:  # paused during the follow-up check: finish that check
            again, more, more_skipped = self._run_member(member, task, deliverables, entry, RECONCILE_STEPS,
                                                         review=review, reconcile=True)
            result, changes, skipped = _after_follow_up(entry["first"], again, more, more_skipped)
            parallel = self._parallel_note(entry["peers"], checked=True)
        else:
            result, changes, skipped = self._run_member(member, task, deliverables, entry, max_steps,
                                                        fresh=fresh, review=review)
            if batch is not None:
                result, changes, skipped, parallel = self._reconcile(batch, member, deliverables, entry, review,
                                                                     result, changes, skipped)
            elif entry.get("peers"):
                parallel = self._parallel_note(entry["peers"], reason="the task was paused while they ran")
        with self.lock:
            self.inflight.pop(assignee, None)
            self.lead_baseline = self.store.project.checkout(self.store.workspace(lead_id))
        outcome = self._close_assignment(member, task, deliverables, result, changes, skipped, checks, review, loop)
        if parallel:
            outcome["parallel"] = parallel
        return outcome

    def _parallel_note(self, peers: list[str], *, checked: bool = False, reason: str = "") -> str:
        names = ", ".join(f"{self.group.member(p).name} ({p})" if self._known(p) else p for p in peers)
        if checked:
            return f"Ran at the same time as {names}; afterwards checked their results (included in the report)."
        return (f"Ran at the same time as {names}. The follow-up check against their results did not run ({reason}). "
                "If this work depends on theirs, delegate a follow-up.")

    def _known(self, member_id: str) -> bool:
        return any(m.id == member_id for m in self.group.members)

    def _reconcile(self, batch: ParallelBatch, member: Member, deliverables, entry, review, result, changes, skipped):
        """After a parallel batch, each teammate checks the others' results (a deferred join).

        Like a LangGraph node with defer=True, this waits until every branch of the batch has finished.
        The lead may have been wrong that the assignments were independent; this step is where a teammate
        replaces assumptions with the others' actual results. Returns (result, changes, skipped, note).
        """
        batch.arrive(member.id, {"name": member.name, "status": result.status,
                                 "report": result.answer or result.error or "(no report)",
                                 "files": [f"{c.path} ({c.change})" for c in changes]})
        peers = entry["peers"]
        if not batch.wait(self.should_cancel):
            reason = "the task was cancelled" if self.should_cancel() else "a teammate's run was paused or failed"
            return result, changes, skipped, self._parallel_note(peers, reason=reason)
        others = [o for key, o in batch.outcomes.items() if key != member.id]
        if result.status in ("error", "cancelled"):
            return result, changes, skipped, self._parallel_note(peers, reason="this run did not finish")
        if not others:
            return result, changes, skipped, self._parallel_note(peers, reason="no teammate's assignment ran")
        lines = ["While you worked, these teammates finished assignments that ran at the same time as yours:"]
        if not self.settings.team.resume_members:
            lines.insert(0, "Your report on your assignment was:\n" + _clip(result.answer or "(no report)", 4000) + "\n")
        for other in others:
            lines.append(f"\n### {other['name']} ({other['status']})\nPublished: "
                         f"{', '.join(other['files']) or 'no files'}\n{_clip(other['report'], 3000)}")
        lines.append("\nYour assignment ran in parallel on the assumption that it needed nothing from theirs. Check "
                     "whether that holds. If their results change anything in your work (numbers, facts, assumptions, "
                     "files you rely on), update your files now and report exactly what you changed. If nothing needs "
                     "to change, reply 'No change needed' and say why in one sentence.")
        text = "\n".join(lines)
        event = self.store.append("delegation", task_id=self.task_id, agent=self.primary, assignee=member.id,
                                  task=text, deliverables=deliverables, reconcile=True)
        # Kept in the in-flight entry, so a pause during the check can finish it on resume.
        entry["first"] = {"status": result.status, "answer": result.answer, "error": result.error,
                          "changes": [c.as_dict() for c in changes], "skipped": list(skipped)}
        entry["seq"] = event["seq"]
        entry.pop("baseline", None)  # check out again so the teammates' files are in the workspace
        again, more, more_skipped = self._run_member(member, text, deliverables, entry, RECONCILE_STEPS,
                                                     review=review, reconcile=True)
        combined, merged, all_skipped = _after_follow_up(entry["first"], again, more, more_skipped,
                                                         first_result=result)
        return combined, merged, all_skipped, self._parallel_note(peers, checked=True)

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
        if outcome.get("parallel"):
            lines.append(outcome["parallel"])
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
        if self.settings.team.fast and type(rounds_max) is int:
            rounds_max = min(rounds_max, 2)
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

    def _time_boxed(self, limits, reserve: float = 0):
        """Within a task time limit, an agent run gets the time that is left (minus room for the lead to wrap up)."""
        if self.deadline is None:
            return limits
        left = max(5.0, self.deadline - time.monotonic() - reserve)  # past the limit an agent only writes its report
        return replace(limits, max_seconds=min(limits.max_seconds or left, left))

    def _update_plan(self, lead_id: str, args: dict) -> ToolResult:
        steps = []
        for item in args.get("steps") or []:
            text = str(item.get("step", "")).strip()
            if not 1 <= len(text) <= 300:
                return ToolResult.failure("Each step needs 1–300 characters of text.")
            row = {"step": text, "status": item.get("status", "pending")}
            if item.get("owner"):
                row["owner"] = str(item["owner"]).strip()[:60]
            after = item.get("after") or []
            if after:
                if any(type(n) is not int or not 1 <= n <= len(steps) for n in after):
                    return ToolResult.failure(f"Step {len(steps) + 1}: `after` may only name earlier steps "
                                              f"(1–{len(steps)}).")
                row["after"] = sorted(set(after))
            steps.append(row)
        if not 1 <= len(steps) <= 20:
            return ToolResult.failure("A plan has 1–20 steps.")
        explanation = str(args.get("explanation") or "").strip()[:600]
        with self.lock:
            self.plan = {"steps": steps, "explanation": explanation, "updated_at": now()}
            self._update(plan=self.plan)
        self.store.append("plan", task_id=self.task_id, agent=lead_id, steps=steps, explanation=explanation)
        done = sum(s["status"] == "completed" for s in steps)
        ready = [str(i) for i, s in enumerate(steps, 1) if s["status"] == "pending"
                 and all(steps[n - 1]["status"] == "completed" for n in s.get("after", []))]
        hint = f" Ready to start: steps {', '.join(ready)}." if len(ready) > 1 else ""
        return ToolResult(ok=True, content=f"Plan updated: {done}/{len(steps)} steps done.{hint} The requester can see it.")

    def _run_member(self, member: Member, task: str, deliverables: list[str], entry: dict, max_steps=None,
                    *, fresh: bool = False, review: bool = False, reconcile: bool = False):
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
        if review:
            assignment += ("\n\nThis assignment is a review or test: you own the verification of this work. Check "
                           "the real behaviour end to end and report concrete evidence.")
        assignment = f"Assignment from {lead.name} ({lead.id}):\n\n" + assignment
        # Reviews keep the lead's thinking (or the reviewer settings); in fast mode members' own work does not think.
        settings = self.settings.reviewer() if review else self.settings.member()
        if review:
            provider = self.reviewer_provider if self.reviewer_provider is not None else self.provider
        else:
            provider = self.member_provider if self.member_provider is not None else self.provider
        limits = settings.limits if max_steps is None else replace(settings.limits, max_steps=max_steps)
        agent = Agent(member.id, provider, self._registry(member.id, lead=False), Sandbox(workspace, member.id),
                      settings, limits=self._time_boxed(limits, reserve=45),
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
        recovered = 0
        try:
            result = agent.run(assignment, context=context, session=session)
            # A failed model request should not cost the member its work: continue the same session.
            while (result.status == "error" and result.error.startswith("model request failed")
                   and session is not None and session.resumable and recovered < MODEL_ERROR_RETRIES
                   and not self.should_cancel()):
                recovered += 1
                time.sleep(MODEL_ERROR_PAUSE)
                agent.limits = replace(agent.limits, max_steps=max(10, agent.limits.max_steps - result.steps))
                again = agent.run("Your previous run on this assignment stopped because a model request failed "
                                  f"({result.error}). Your conversation and your workspace are intact. Continue "
                                  "the same assignment from where you left off and finish with your report.",
                                  session=session)
                result = _combined(result, again)
            if result.status == "error" and not result.answer and session is not None:
                note = _last_note(session.messages)
                if note:
                    result = replace(result, answer="(No final report: the run failed.) Last progress note: " + note)
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
            if recovered:
                metrics["recovered_after_error"] = recovered
            if reconcile:
                metrics["reconcile"] = True
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
        self.meter.started = time.time() - self.control.saved.get("elapsed", 0)
        self.meter.budget_minutes = float(self.options.get("budget_minutes")
                                          or (FAST_SOFT_MINUTES if self.settings.team.fast else 0))
        if self.settings.team.task_seconds:
            self.deadline = time.monotonic() + self.settings.team.task_seconds - self.control.saved.get("elapsed", 0)
        self.meter.budget_tokens = int(self.options.get("budget_tokens") or 0)
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
                              **({"continued_from":self.continued_from} if self.continued_from else {}),
                              **({"fast": True} if self.settings.team.fast else {}))
            self.lead_baseline = self.store.project.checkout(workspace)
        settings = self.settings
        # The coordinating lead gets the lead budget; a member addressed directly works
        # like any assignment, with the member budget.
        limits = replace(settings.limits, max_steps=settings.team.lead_max_steps) if can_delegate else settings.limits
        limits = self._time_boxed(limits)
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
                   "resumed_assignments": sum(1 for a in self.assignments if a.get("resumed") and not a.get("reconcile")),
                   "parallel_checks": sum(1 for a in self.assignments if a.get("reconcile")),
                   "recovered_after_error": sum(a.get("recovered_after_error", 0) for a in self.assignments),
                   "review_assignments": sum(1 for a in self.assignments if a.get("review")),
                   "parallel_batches": self.parallel_batches, "parallel_reads": self.parallel_reads,
                   "time": {k: v for k, v in self.meter.snapshot().items() if k != "alerts"},
                   "steps": totals["steps"] + sum(a["steps"] for a in self.assignments),
                   "tool_calls": totals["tool_calls"] + sum(a["tool_calls"] for a in self.assignments)}
        self._update(status=status, finished_at=now(), final_answer=answer, error=result.error,
                     active=[], metrics=metrics, assignments=self.assignments, delegations=self.delegations)
        self.store.append("task_finished", task_id=self.task_id, status=status, metrics=metrics, error=result.error)
        return self.record


def _combined(first: AgentResult, second: AgentResult) -> AgentResult:
    """One assignment that needed a second run of the same session."""
    return replace(second, steps=first.steps + second.steps, tool_calls=first.tool_calls + second.tool_calls,
                   usage=first.usage.merged(second.usage), duration_s=round(first.duration_s + second.duration_s, 2),
                   compactions=first.compactions + second.compactions, resumed=first.resumed)


def _after_follow_up(first: dict, again: AgentResult, more: list, more_skipped: list, *,
                     first_result: AgentResult | None = None):
    """One report for an assignment and its parallel follow-up check."""
    merged = {c["path"]: Change(**c) for c in first["changes"]}
    for change in more:  # as seen from before the assignment: a file it added and then edited is "added"
        earlier = merged.get(change.path)
        if earlier is not None and earlier.change == "added" and change.change == "deleted":
            del merged[change.path]
        elif earlier is not None and earlier.change == "added":
            merged[change.path] = replace(change, change="added", conflict=earlier.conflict or change.conflict)
        else:
            merged[change.path] = change
    answer = ((first["answer"] or "(no report)") + "\n\nAfter seeing the parallel teammates' results: "
              + (again.answer or again.error or "(no reply)"))
    base = _combined(first_result, again) if first_result is not None else again
    status = first["status"] if again.status == "completed" else again.status
    result = replace(base, answer=answer, status=status, error=again.error or first["error"])
    return result, list(merged.values()), list(first["skipped"]) + list(more_skipped)


def _last_note(messages: list[dict]) -> str:
    for message in reversed(messages):
        if (message.get("role") == "assistant" and (message.get("content") or "").strip()
                and message["content"] != FAILED_REQUEST_NOTE):
            return _clip(message["content"].strip(), 1500)
    return ""


def _metrics(agent: str, result: AgentResult) -> dict:
    return {"agent": agent, "status": result.status, "steps": result.steps, "tool_calls": result.tool_calls,
            "prompt_tokens": result.usage.prompt_tokens, "completion_tokens": result.usage.completion_tokens,
            "prompt_cache_hit_tokens": result.usage.prompt_cache_hit_tokens or 0,
            "duration_s": result.duration_s, "compactions": result.compactions, "error": result.error,
            "resumed": result.resumed}
