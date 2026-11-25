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
from .browser_evidence import format_report
from .plan_view import evidence_steps, identify_steps, plan_view
from .review import VERDICT_LINE, review_verdict
from .checkpoint import Continuation, PauseRequested
from .config import FAST_SOFT_MINUTES, HANDOFF_KINDS, KIND_PROFILES, Settings, kind_settings
from .context import ContextManager
from .evaluation import evaluate, validate
from .mailbox import Cancelled, HumanChannel
from .consult import LeadConsult
from .meter import TaskMeter
from .providers import Provider
from .sandbox import Sandbox
from . import handoffs, recovery, team_talk
from .schemas import ToolResult, Usage
from .store import GroupStore, Member, now
from .tools import Tool, ToolRegistry, build_default_registry, validate_arguments
from .workspace import Change, listing

REPORT_LIMIT = 14_000
MODEL_ERROR_RETRIES = 1
"""After a failed model request, a member continues its own session this many times before reporting."""
MODEL_ERROR_PAUSE = 5.0
"""Seconds to wait before continuing after a failed model request."""
PLAN_NUDGE_STEP = 6
"""Step at which a coordinating lead with no plan and no delegations is reminded once to plan and delegate."""


class Claim:
    """One hand-off's hold on its teammates and files, from dispatch until it reports.

    Every hand-off in a response starts at once. A hand-off waits only for an earlier, unfinished one
    that it conflicts with: the same teammate, a file both would write, or a file listed in this one's
    ``inputs`` that the earlier one produces. Only declared files count — a brief that names another
    hand-off's file (often to mark a boundary: "X covers that in a.md, do not duplicate it") does not
    make it wait. Earlier means registered earlier, so the waits form no cycle.
    """

    def __init__(self, label: str, members, writes, reads, background: bool = False):
        self.label = label
        self.members = set(members)
        self.writes = [p for p in (_norm(w) for w in writes) if p]
        self.reads = [p for p in (_norm(r) for r in reads) if p]
        self.background = background
        self.started = False
        self.taken = True
        """False while announced for a response but not yet picked up by its call."""
        self.created = time.monotonic()
        self.in_thread = False
        """A background hand-off: its run, not its tool call, releases the claim."""
        self.done = threading.Event()

    def conflict(self, later: "Claim") -> tuple[str, str] | None:
        """Why ``later`` must wait for this claim — ``("member", ids)``, ``("write", path)`` or
        ``("read", path)`` — or None when they may overlap."""
        shared = sorted(self.members & later.members)
        if shared:
            return "member", ", ".join(shared)
        clash = next((w for w in later.writes for mine in self.writes if _overlaps(w, mine)), None)
        if clash:
            return "write", clash
        read = next((r for r in later.reads for mine in self.writes if _overlaps(r, mine)), None)
        if read:
            return "read", read
        return None

    def why(self, reason: str, subject: str) -> str:
        return {"member": f"{subject} works on one assignment at a time",
                "write": f"both write {subject}",
                "read": f"it reads {subject}, which {self.label} produces"}[reason]


ANNOUNCE_GRACE = 5.0
"""Seconds after which a hand-off announced for a response but never picked up stops blocking others."""


def _claim_args(name: str, args: dict) -> tuple:
    """A stable key for one hand-off call, to find the claim registered when its response arrived."""
    return name, json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)


PLAN_STEP_IDS = {"type":"array", "items":{"type":"string"}, "maxItems":20,
                 "description":"All stable plan step IDs this single handoff covers"}

CHECKS_SCHEMA = {
    "type": "array", "maxItems": 20,
    "description": ("Mechanical checks Studio runs on the published project after the work, for your information: "
                    "{kind: exists, path} | {kind: contains, path, text} | {kind: command, command} (passes on exit 0)"),
    "items": {"type": "object", "properties": {
        "kind": {"type": "string", "enum": ["exists", "contains", "command"]},
        "path": {"type": "string"}, "text": {"type": "string"}, "command": {"type": "string"}},
        "required": ["kind"]},
}


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
                "- Work that can overlap goes in one response: every `delegate_task` and `review_loop` you send in "
                "the same response starts at once. Studio makes one wait only when it must — the same teammate, a "
                "file both would write, or a file in its `inputs` that another one in progress produces — and the "
                "chat and its result say what it waited for. So declare `deliverables`, list hard prerequisites in "
                "`inputs`, and put non-blocking background files in `reference_files` (preloaded without waiting); "
                "a file only named in the brief (to mark a boundary, say) does not hold it "
                "up. Good to overlap: release notes, a user guide or a test plan drafted while the build or the "
                "tests run; QA testing a finished build while the docs are written from it; separate research topics "
                "for separate researchers. Not: anything that reviews, summarises, prices or presents work that does "
                "not exist yet — a risk review before the research, an outline before the reporting, client material "
                "before the analysis; send that after the work it needs. When a report comes back, Studio names the plan "
                "steps that can start together.")
        parts.append(
            "- Add `checks` to an assignment when 'done' can be checked mechanically (a file exists, contains a "
            "phrase, a command exits 0). Studio runs them on the published project after the teammate finishes and "
            "shows you the results; they inform your judgement and never block delivery. Set `review: true` on "
            "an assignment that reviews or tests someone else's work.")
        parts += handoffs.lead_guidance(kinds=settings.team.handoff_kinds, background=settings.team.background_handoffs)
        if settings.team.review_loops and len(members) > 2:
            parts.append(
                "- For an author–reviewer cycle (build → test → fix → re-test), `review_loop` runs the rounds for you: "
                "the reviewer's findings go straight to the author's continued session, and you receive the final "
                "verdict, the last review and the files changed.")
        if settings.team.plans and settings.team.plan_links:
            parts.append("- Write the plan once. For your own steps, bind lead_outputs or on_delivery. Pass the covered "
                         "step IDs on handoffs and retest_steps for post-fix reviews. Use record_plan_result for a decision "
                         "on individual steps. Give a step `after` (earlier step IDs or numbers) when it needs earlier steps; steps "
                         "whose `after` steps are done can be started together, and Studio names them when a report "
                         "comes back. Dependencies are kept when a later update omits `after`.")
        elif settings.team.plans:
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
             "mode for that work (for example \"用快速模式\" or \"fast mode\"), never on your own judgement. Likewise set "
             "`options.thinking` or `options.reasoning_effort` (low, high, max) only when the requester asks for them." if studio_assistant else
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
    if can_delegate and team_talk.enabled(settings):
        parts.append("For longer assignments, name milestones where a check-in would change your decision. "
                     "Members can ask each other factual questions and exchange notes.")
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
    if team_talk.enabled(settings):
        if settings.team.lead_consult:
            parts.append("Use check_in for a blocker, an unsettled choice, a deviation or a requested milestone.")
        if len(members) > 2:
            parts.append("Ask a teammate about evidence they hold or send a note that changes their work.")
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
    marks = {"completed": "[x]", "in_progress": "[~]", "pending": "[ ]", "rework": "[!]",
             "incomplete": "[incomplete]",
             "submitted": "[submitted]", "unconfirmed": "[submitted, unconfirmed]", "unrecorded": "[no recorded outcome]"}
    steps = plan.get("steps", [])
    numbered = any(s.get("after") for s in steps)
    rows = [f"{marks.get(s.get('stage', s['status']), '[ ]')} " + (f"{i}. " if numbered else "") + s["step"]
            + (f" — {s['owner']}" if s.get("owner") else "")
            + (f" (after {', '.join(map(str, s['after']))})" if s.get("after") else "")
            for i, s in enumerate(steps, 1)]
    for i, step in enumerate(steps):
        if step.get("id"):
            rows[i] += f" (ID: {step['id']})"
        evidence = step.get("evidence", {})
        if assigned := evidence.get("assigned"):
            rows[i] += f" — assigned to {assigned['agent']}"
        if submitted := evidence.get("submitted"):
            paths = ", ".join(f["path"] for f in submitted["files"]) or "report"
            rows[i] += f" — submitted by {submitted['agent']}: {paths}"
        if reviewed := evidence.get("reviewed"):
            rows[i] += f" — review: {reviewed['verdict']}"
        if step.get("completed"):
            rows[i] += " — completed" + (" and confirmed" if step.get("confirmed") else ", awaiting confirmation")
    return "\n".join(rows) + (f"\nNote: {plan['explanation']}" if plan.get("explanation") else "")


def browser_handoff(checks: dict) -> str:
    rows = [f"Browser evidence from {checks['calls']} call(s) in this assignment:"]
    for check in checks["findings"]:
        rows.append(f"Call {check['number']} ({check['call_id']})\n" + format_report(check["report"]))
    return "\n".join(rows)


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


def conversation(store: GroupStore, before_seq: int, budget_chars: int = 48_000, *, after_seq: int = 0, reader: str | None = None, mail_task: str | None = None) -> str:
    names = {m.id: m.name for m in store.group.members}
    lines: list[str] = []
    used = 0
    for event in reversed(store.chat()):
        if event["seq"] >= before_seq:
            continue
        if event["seq"] <= after_seq:
            break
        if reader and event.get("recipients") and event["kind"] == "user_message":
            # Directed instructions enter the recipient's current task once via its inbox.
            if reader not in event["recipients"] or event.get("task_id") == mail_task:
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


def agent_context(store: GroupStore, agent_id: str, before_seq: int, *, mail_task=None) -> str:
    sections = []
    chat = conversation(store, before_seq, reader=agent_id, mail_task=mail_task)
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
        self.talk = team_talk.TeamTalk(self) if team_talk.enabled(settings) else None
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
        self.parallel_reads = 0
        self.claims: list[Claim] = []
        """Hand-offs waiting or running, in dispatch order (see Claim)."""
        self.announced: dict[tuple, list[Claim]] = {}
        """Claims registered for a response's hand-offs before their threads start, so order is kept."""
        self.waiting_turns = 0
        self.bg_claims: dict[str, Claim] = {}
        self.meter = TaskMeter(notify=lambda text: self.channel.note(self.primary, text))
        self.active: list[str] = []
        self.active_modes: dict[str, dict[str, Any]] = {}
        self.record: dict[str, Any] = {}
        self.control = Continuation()
        self.inflight: dict[str, dict] = {}
        """Assignments started but not finished, by assignee; a resumed task rejoins them."""
        self.loop_state: dict | None = None
        """Progress of the review loop in flight, so a resumed task continues the same round."""
        self.started_at = None
        self.lead_session = AgentSession()
        self.provider_factory: Callable[[Settings], Provider] | None = None
        """Builds providers for hand-off kinds whose thinking or effort differ, cached per setting."""
        self.providers: dict[tuple, Provider] = {}
        self.thinking_pinned = False
        """The task's options set thinking explicitly, so hand-off kinds do not change it."""
        self.effort_pinned = False
        """The task's options set reasoning_effort explicitly, so hand-off kinds do not lower it."""
        self.background: dict[str, dict] = {}
        """Hand-offs running in the background, by assignee (the thread and its declared files)."""
        self.bg_cond = threading.Condition()
        self.bg_finished: list[str] = []
        """Background reports collected while the lead waits or after it finished."""
        self.bg_untaken = 0
        """Background reports delivered to the lead's inbox that it has not read yet."""
        self.waiting_handoffs = False
        self.lead_live = False
        self.can_delegate = False
        self.plan_nudged = False
        self.lead_stale = False
        """Background hand-offs published files after the lead's workspace was last refreshed."""
        self.evidence: list[dict] = []
        """Finished hand-offs, for the check, fix and adapt hand-offs that follow (handoffs.evidence_block)."""
        self.consults = LeadConsult(self)
        self.channel.primary = primary
        self.channel.enabled = settings.team.mailboxes
        self.channel.interrupts = settings.team.mailboxes and settings.team.interrupts
        self.channel.on_event = self._event
        self.channel.on_user_mail = self.consults.schedule
        self.channel.before_take = self._refresh_lead

    def consult_lead(self, mail, *, checkin=False):
        return self.consults.decide(mail, checkin=checkin)

    def continuation_state(self):
        return {"task_id":self.task_id, "meter":self.meter.checkpoint(), "elapsed":time.monotonic()-self.started_at, "frames":self.control.frames, "team":self.group.to_dict(),
                "lead_baseline":self.lead_baseline, "inflight":self.inflight, "loop_state":self.loop_state,
                "sessions":{f"{member}|{kind}": {"messages":session.messages, "runs":session.runs,
                                                 "context":asdict(session.context) if session.context else None}
                            for (member, kind), session in self.sessions.items()},
                "session_marks":{f"{member}|{kind}": mark for (member, kind), mark in self.session_marks.items()},
                "plan":self.plan, "parallel_batches":self.parallel_batches,
                "delegations":self.delegations, "history":self.history, "assignments":self.assignments,
                "channel":self.channel.snapshot(), "consult_calls": self.consults.calls,
                "bg_finished": list(self.bg_finished), "bg_untaken": self.bg_untaken, "evidence": self.evidence}


    # -- plumbing ---------------------------------------------------------
    def _event(self, event: dict) -> None:
        event = {"task_id": self.task_id, **event}
        if event["type"] in ("start","resume"):
            self.channel.activate(event["agent"])
            with self.lock:
                inflight = self.inflight.get(event["agent"])
                handoff_kind = inflight.get("kind") if inflight else None
                if handoff_kind:
                    event["handoff_kind"] = handoff_kind
                if "thinking" in event or handoff_kind:
                    mode = {
                        "model": event.get("model"),
                        "thinking": event.get("thinking"),
                        "reasoning_effort": event.get("reasoning_effort"),
                    }
                    if handoff_kind:
                        mode["handoff_kind"] = handoff_kind
                    self.active_modes[event["agent"]] = mode
                    self._update(active_modes=dict(self.active_modes))
        if event["type"] in ("start", "resume") and event.get("agent") == self.primary:
            self.lead_live = True
        if event["type"] == "end":
            self.channel.deactivate(event["agent"])
            with self.lock:
                if self.active_modes.pop(event["agent"], None) is not None:
                    self._update(active_modes=dict(self.active_modes))
            if event.get("agent") == self.primary:
                with self.bg_cond:
                    self.lead_live = False
                    still_open = bool(self.background or self.bg_finished or self.bg_untaken)
                if not still_open:  # otherwise the lead runs again for the background reports (see run)
                    self.consults.close()
                    self.channel.finish()
        if (self.talk and event["type"] == "step" and event.get("max_steps")
                and event["step"] >= 0.6 * event["max_steps"]):
            self.talk.silence(event["agent"])
        if (event["type"] == "step" and event.get("agent") == self.primary
                and self.can_delegate and self.settings.team.plans
                and not self.plan_nudged and not self.plan and self.delegations == 0
                and event.get("step", 0) >= PLAN_NUDGE_STEP):
            self.plan_nudged = True
            self.channel.note(
                self.primary,
                "[Studio] You have run several steps without setting a plan or delegating to your team. "
                "Call update_plan now with clear step IDs and dependencies (`after`), then delegate "
                "deep research or execution to teammates with delegate_task instead of doing everything yourself.",
            )
            self.store.trace(self.task_id, {"task_id": self.task_id, "type": "plan_nudge",
                                            "agent": self.primary, "step": event.get("step")})
        if event["type"] == "say":
            self.store.append("progress", task_id=self.task_id, agent=event["agent"], text=event["text"])
        if event["type"] == "tool_result" and event.get("tool") == "run_browser":
            with self.lock:
                entry = self.inflight.get(event["agent"])
                if entry is not None:
                    checks = entry.setdefault("browser_checks", {"calls": 0, "findings": []})
                    checks["calls"] += 1
                    if event.get("browser_report"):
                        checks["findings"].append({"number": checks["calls"], "call_id": event["call_id"],
                                                  "report": event["browser_report"]})
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
        if self.talk and not lead and agent_id != self.primary:
            for tool in self.talk.tools(agent_id):
                registry.add(tool)
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
                if self.settings.team.plan_links:
                    registry.add(self._plan_result_tool(agent_id))
            if self.settings.team.background_handoffs:
                registry.add(self._wait_tool(agent_id))
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

    def _member_plan(self) -> str:
        if self.plan.get("linked"):
            return render_plan(plan_view(self.record, self.store.chat(self.trigger_seq - 1)))
        return render_plan(self.plan)

    def _resume_context(self, key: tuple[str, str], baseline: dict[str, str], through: int) -> str:
        """What a continued member needs to know: the conversation and file changes since its last turn."""
        mark = self.session_marks[key]
        sections = []
        chat = conversation(self.store, through, 24_000, after_seq=mark["seq"], reader=key[0], mail_task=self.task_id)
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
            sections.append("## Team plan (kept by the lead)\n" + self._member_plan())
        sections.append("Your workspace has been refreshed to the current team project. Background processes from "
                        "your previous assignment (servers, watchers) were stopped; start again anything you need.\n"
                        f"Current date and time: {time.strftime('%Y-%m-%d %H:%M %Z')}.")
        return "\n\n".join(sections)

    def _context(self, agent, through):
        context = agent_context(self.store, agent, through, mail_task=self.task_id)
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
            context += "\n\n## Team plan (kept by the lead)\n" + self._member_plan()
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
            uploads = [r for event in self.store.chat() if event["seq"] <= through
                       and (not event.get("recipients") or agent in event["recipients"]) for r in event.get("attachments", [])]
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
                       "description": ("Project-relative files or folders this assignment depends on for their latest "
                                       "version; it waits for any in-progress hand-off that produces one of them and preloads them")},
            "reference_files": {"type": "array", "items": {"type": "string"}, "maxItems": 40,
                                "description": ("Project-relative background files or folders to preload as-is without "
                                                "waiting on other in-progress hand-offs")},
        }
        if team.plans and team.plan_links:
            properties["step"] = {"type": "string", "description": "Primary plan step this hand-off advances"}
            properties["step_ids"] = PLAN_STEP_IDS
        if team.handoff_kinds:
            properties["kind"] = {
                "type": "string", "enum": list(HANDOFF_KINDS),
                "description": ("Shape of the work: research (find out), make (produce a deliverable), check (review "
                                "or test someone else's work), fix (address findings), adapt (turn finished work into "
                                "other material). Sets thinking, step budget and tools.")}
        if team.background_handoffs:
            properties["background"] = {
                "type": "boolean",
                "description": "Return at once instead of waiting for the report; it arrives as a message later"}
        if team.resume_members:
            properties["fresh"] = {"type": "boolean",
                                   "description": "Start the teammate in a clean session instead of continuing "
                                                  "their earlier session in this task (default false)"}
        return Tool(
            name="delegate_task",
            description=("Give one assignment to one teammate and wait for their report. The teammate works in a "
                         "private copy of the team project; their file changes are published to the project when "
                         "they finish. Returns their report, the published files and the results of any checks."
                         + (" Hand-offs in one response start together; one waits only for another it conflicts "
                            "with (same teammate, a file both write, or a file in its inputs that the other produces)."
                            if team.parallel_delegations else "")
                         + (" With background: true it returns at once and the report arrives later."
                            if team.background_handoffs else "")),
            parameters=self._delegate_schema(properties),
            fn=lambda _ctx, args: self._delegate(lead_id, args),
            concurrent=self._start_together if team.parallel_delegations else None,
            phase=1 if team.parallel_delegations else 0)

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
                **({"step": {"type": "string", "description": "Primary plan step for every round"},
                    "step_ids": PLAN_STEP_IDS,
                    "retest_steps": {**PLAN_STEP_IDS, "description":"Additional steps covered only by a review after a fix"}}
                   if self.settings.team.plans and self.settings.team.plan_links else {}),
                "criteria": {"type": "string", "description": "What 'acceptable' means for the reviewer's verdict"},
                "deliverables": {"type": "array", "items": {"type": "string"},
                                 "description": "Project-relative paths the author may change while fixing"},
                "checks": CHECKS_SCHEMA,
                "max_rounds": {"type": "integer", "minimum": 1, "maximum": 5,
                               "description": "Most review rounds (default 3)"},
            }, "required": ["author", "reviewer", "work"]},
            fn=lambda _ctx, args: self._review(lead_id, args),
            concurrent=self._start_together if self.settings.team.parallel_delegations else None,
            phase=1 if self.settings.team.parallel_delegations else 0)

    def _wait_tool(self, lead_id: str) -> Tool:
        return Tool(
            name="wait_handoffs",
            description=("Wait until hand-offs running in the background finish and return their reports. Use it "
                         "when your next step needs their results, or when you have nothing else to do — before "
                         "your final answer."),
            parameters={"type": "object", "properties": {
                "assignees": {"type": "array", "items": {"type": "string", "enum": self._member_ids(lead_id)},
                              "description": "Teammates to wait for (default: every hand-off still running)"}}},
            fn=lambda _ctx, args: self._wait_handoffs(args), phase=2)

    def _link_lead_prereq_publish(self, lead_id: str, link: dict, inputs: list[str]) -> None:
        """If this hand-off reads files the lead just published, link that publication to the sole unclosed
        lead-owned prerequisite step that did not declare explicit outputs."""
        if not (self.settings.team.plan_links and self.plan and self.plan.get("linked") and inputs):
            return
        wanted = link.get("step_ids") or ([link["step"]] if link.get("step") else [])
        if not wanted:
            return
        events = self.store.chat(self.trigger_seq - 1)
        view = plan_view(self.record, events)
        if not view:
            return
        rows = view["steps"]
        by_id = {row["id"]: row for row in rows}
        candidates: dict[str, dict] = {}
        for key in wanted:
            target = by_id.get(key)
            if not target:
                continue
            for dep in target.get("after") or []:
                if not (isinstance(dep, int) and 1 <= dep <= len(rows)):
                    continue
                prereq = rows[dep - 1]
                if (not prereq.get("completed") and prereq.get("stage") not in ("completed", "submitted")
                        and prereq.get("owner") in (None, lead_id) and not (prereq.get("lead_outputs") or prereq.get("outputs"))):
                    candidates[prereq["id"]] = prereq
        if len(candidates) != 1:
            return
        prereq_id = next(iter(candidates))
        norm_inputs = [_norm(p) for p in inputs if _norm(p)]
        for event in reversed(events):
            if event.get("task_id") != self.task_id or event.get("kind") != "publish" or event.get("agent") != lead_id:
                continue
            published = [_norm(c.get("path", "")) for c in event.get("changes", []) if c.get("change") != "deleted"]
            if any(_overlaps(pub, inp) for pub in published if pub for inp in norm_inputs):
                self.store.append("plan_evidence", task_id=self.task_id, agent=lead_id,
                                  links=[{"source_seq": event["seq"], "step_ids": [prereq_id]}])
                return

    def _unclosed_lead_steps(self, lead_id: str, link: dict) -> str:
        """A soft reminder when this hand-off follows a step of the lead's own that the plan does not show as done.

        Nothing is refused: the plan, not the work, may be what is wrong (in S2T1-v3.4 the brief existed at another
        path than the step declared). The lead is shown what the step expects and what it actually published.
        """
        if not (self.settings.team.plan_links and self.plan and self.plan.get("linked")):
            return ""
        wanted = link.get("step_ids") or ([link["step"]] if link.get("step") else [])
        if not wanted:
            return ""
        view = plan_view(self.record, self.store.chat(self.trigger_seq - 1))
        if not view:
            return ""
        rows = view["steps"]
        numbers = {row.get("id"): n for n, row in enumerate(rows, 1)}
        needed = sorted({k for key in wanted if key in numbers for k in rows[numbers[key] - 1].get("after", [])})
        open_steps = [rows[k - 1] for k in needed if 1 <= k <= len(rows)
                      and rows[k - 1].get("stage") != "completed" and rows[k - 1].get("owner") in (None, "", lead_id)]
        if not open_steps:
            return ""
        manifest = self.store.project.manifest()
        parts = []
        for row in open_steps:
            absent = [path for path in (row.get("lead_outputs") or row.get("outputs") or []) if not _present(path, manifest)]
            detail = f"; expects {', '.join(absent)}, which is not in the project" if absent else ""
            parts.append(f"{row['id']} ({_clip(row['step'], 70)}{detail})")
        events = self.store.chat(self.trigger_seq - 1)
        plan_seq = max((e["seq"] for e in events if e["kind"] == "plan" and e.get("task_id") == self.task_id), default=0)
        published = list(dict.fromkeys(change["path"] for e in events
                                       if e["kind"] == "publish" and e.get("task_id") == self.task_id
                                       and e.get("agent") == lead_id and e["seq"] > plan_seq
                                       for change in e.get("changes", []) if change.get("change") != "deleted"))
        line = ("Plan: this hand-off follows your own step " + "; ".join(parts)
                + (", which the plan does not show as done." if len(parts) == 1 else ". The plan shows none of them as done."))
        if published:
            line += " Files you published since the plan was written: " + ", ".join(published[:8]) + "."
        line += " Close the step with record_plan_result, or correct its lead_outputs with update_plan."
        return line

    def _ready_note(self) -> str:
        """Plan steps that can start now, read from the recorded hand-offs; costs no model call.

        Studio never starts plan steps itself (the lead decides); it only points out, when a report comes
        back, the steps whose dependencies are done so independent ones are not run one after another.
        """
        team = self.settings.team
        if not (team.plans and team.plan_links and self.plan and self.plan.get("linked")):
            return ""
        steps = self.plan.get("steps", [])
        if not any(s.get("after") for s in steps):
            return ""  # without dependencies Studio cannot tell what is independent
        view = plan_view(self.record, self.store.chat(self.trigger_seq - 1))
        if not view:
            return ""
        rows = view["steps"]
        stages = [row.get("stage", row["status"]) for row in rows]
        with self.lock:
            busy = set(self.inflight)
        ready = [(n, row) for n, row in enumerate(rows, 1) if stages[n - 1] == "pending"
                 and all(stages[k - 1] == "completed" for k in row.get("after", []) if 1 <= k <= len(rows))
                 and row.get("owner") not in busy]
        owners = {row.get("owner") or f"step {n}" for n, row in ready}
        if len(ready) < 2 or len(owners) < 2:
            return ""
        names = "; ".join(f"{n} {row.get('id', '')} ({row.get('owner') or 'no owner'})".replace("  ", " ")
                          for n, row in ready)
        has_lead = any(row.get("owner") == self.primary for _, row in ready)
        has_member = any(row.get("owner") not in (None, "", self.primary) for _, row in ready)
        if has_lead and has_member and team.background_handoffs:
            how = (" Delegate the teammate step(s) with `background: true` first, then work on your own step "
                   "while they run (put non-blocking background files in `reference_files`, not `inputs`).")
        else:
            ways = []
            if team.parallel_delegations:
                ways.append("by sending all of them in one response, each with its own deliverables")
            if team.background_handoffs:
                ways.append("start one with `background: true` and keep working")
            how = (" Start them " + ", or ".join(ways) + " — put non-blocking background files in `reference_files` "
                   "so they preload without waiting.") if ways else ""
        return f"Plan: steps {names} can start now — their dependencies are done and none needs another.{how}"

    def lead_suspended(self) -> bool:
        """The lead is inside a synchronous hand-off, a review loop or wait_handoffs, so it is not reading mail."""
        with self.lock:
            return (self.waiting_handoffs or self.waiting_turns > 0
                    or any(not row.get("background") for row in self.inflight.values()))

    def _wants_background(self, args: dict) -> bool:
        if not self.settings.team.background_handoffs:
            return False
        return args.get("background") is True

    def _provider_for(self, settings: Settings, review: bool) -> Provider:
        """One provider per (model, thinking, effort); the task's own providers are reused when they match."""
        key = (settings.model, settings.thinking, settings.reasoning_effort)
        known = [(self.settings, self.provider)]
        if self.reviewer_provider is not None:
            known.append((self.settings.reviewer(), self.reviewer_provider))
        if self.member_provider is not None:
            known.append((self.settings.member(), self.member_provider))
        for base, provider in known:
            if (base.model, base.thinking, base.reasoning_effort) == key:
                return provider
        if self.provider_factory is None:
            fallback = self.reviewer_provider if review else self.member_provider
            return fallback if fallback is not None else self.provider
        with self.lock:
            if key not in self.providers:
                self.providers[key] = self.provider_factory(settings)
            return self.providers[key]

    def _refresh_lead(self, agent: str) -> None:
        """Before the lead takes its mail: its notes are read now, and its workspace takes in new files."""
        if agent != self.primary:
            return
        with self.bg_cond:
            self.bg_untaken = 0
        self._refresh_workspace()

    def _refresh_workspace(self) -> None:
        """At a lead step boundary, bring in what background hand-offs published (the lead's own edits go first)."""
        if not self.lead_stale:
            return
        with self.lock:
            self.lead_stale = False
            workspace = self.store.workspace(self.primary)
            own, _ = self.store.project.publish(workspace, self.lead_baseline, author=self.primary,
                                                task_id=self.task_id, note="lead edits before a background report",
                                                conflicts_dir=self.store.task_dir(self.task_id) / "conflicts")
            if own:
                self.store.append("publish", task_id=self.task_id, agent=self.primary,
                                  changes=[c.as_dict() for c in own])
            self.lead_baseline = self.store.project.checkout(workspace)

    def _start_background(self, entry: dict, claim: Claim | None = None) -> None:
        assignee = entry["assignee"]
        if claim is None:  # a resumed task: the hand-off holds its teammate and files again
            claim = self._register(Claim(assignee, [assignee], entry["run"]["deliverables"], entry.get("inputs") or [],
                                         background=True))
        claim.started = True
        claim.in_thread = True
        self.bg_claims[assignee] = claim
        thread = threading.Thread(target=self._background_run, args=(entry,), daemon=True,
                                  name=f"handoff-{self.task_id}-{assignee}")
        with self.bg_cond:
            self.background[assignee] = {"thread": thread, "deliverables": entry["run"]["deliverables"],
                                         "seq": entry["seq"]}
        thread.start()

    def _background_run(self, entry: dict) -> None:
        """A hand-off in its own thread, like a Codex spawned agent; the lead collects it with wait_handoffs."""
        run, assignee = entry["run"], entry["assignee"]
        member = self.group.member(assignee)
        text = ""
        try:
            result, changes, skipped = self._run_member(member, run["task"], run["deliverables"], entry,
                                                        run["max_steps"], fresh=run["fresh"], review=run["review"])
            with self.lock:
                self.inflight.pop(assignee, None)
            outcome = self._close_assignment(member, run["task"], run["deliverables"], result, changes, skipped,
                                             run["checks"], run["review"], None, entry.get("browser_checks"), entry)
            lines = self._report_lines(outcome)
            lines[0] = "Background hand-off finished. " + lines[0]
            if ready := self._ready_note():
                lines.append(ready)
            lines.append(f"Delegations used: {self.delegations}/{self.settings.team.max_delegations}.")
            text = "\n".join(lines)
        except PauseRequested:
            pass  # the entry stays in flight with the member's frame; the resumed task starts it again
        except Exception as exc:  # noqa: BLE001 - report the failure instead of losing the hand-off
            with self.lock:
                self.inflight.pop(assignee, None)
            text = f"Background hand-off to {member.name} ({assignee}) failed: {exc}"
            self.store.append("report", task_id=self.task_id, agent=assignee, status="error", text=text, changes=[],
                              steps=0, tool_calls=0, background=True,
                              **({"handoff_kind": entry["kind"]} if entry.get("kind") else {}))
        finally:
            self._release(self.bg_claims.pop(assignee, None))
            self._background_finished(assignee, text)

    def _background_finished(self, assignee: str, text: str) -> None:
        with self.bg_cond:
            self.background.pop(assignee, None)
            if text:
                self.lead_stale = True
                if self.waiting_handoffs or not self.lead_live:
                    self.bg_finished.append(text)
                else:
                    self.bg_untaken += 1
                    self.channel.deliver(self.primary, text)
            self.bg_cond.notify_all()

    def _wait_handoffs(self, args: dict) -> ToolResult:
        wanted = [str(a).strip() for a in args.get("assignees") or [] if str(a).strip()]
        with self.bg_cond:
            targets = [a for a in (wanted or list(self.background)) if a in self.background]
            if not targets and not self.bg_finished:
                return ToolResult(ok=True, content=(
                    "No hand-off is running in the background" + (f" for {', '.join(wanted)}" if wanted else "")
                    + ". Reports of finished ones arrive as messages at your next step."))
            self.waiting_handoffs = True
            try:
                while True:
                    # A member stopped by a pause leaves without a report: the wait pauses too and resumes with it.
                    self.control.boundary()
                    if not any(a in self.background for a in targets) or self.should_cancel():
                        break
                    if self.deadline is not None and time.monotonic() >= self.deadline:
                        break
                    self.bg_cond.wait(0.5)
                texts, self.bg_finished = self.bg_finished, []
            finally:
                self.waiting_handoffs = False
            still = sorted(self.background)
        self._refresh_workspace()
        lines = texts or ["(No new reports.)"]
        if still:
            lines.append("Still running in the background: " + ", ".join(still) + ".")
        return ToolResult(ok=True, content="\n\n".join(lines))

    def _join_background(self, timeout: float | None = None) -> None:
        with self.bg_cond:
            threads = [row["thread"] for row in self.background.values()]
        for thread in threads:
            thread.join(timeout)

    def _after_background(self, agent: Agent, result: AgentResult) -> AgentResult:
        """The lead answered while background hand-offs were open: it waits for them and may update its answer.

        The wait is a ``wait_handoffs`` call inside the lead's own loop, so a pause during it saves the
        lead's frame like any other tool call and the resumed task continues the wait.
        """
        for round_ in range(2):
            with self.bg_cond:
                still_open = bool(self.background or self.bg_finished or self.bg_untaken)
            with self.lock:  # a hand-off stopped by a pause is still in flight
                still_open = still_open or any(row.get("background") for row in self.inflight.values())
            if not still_open or self.should_cancel() or result.status in ("cancelled", "error", "dispatched"):
                return result
            agent.limits = self._time_boxed(replace(agent.limits, max_steps=8))
            note = ("[Studio] You answered while hand-offs you started in the background were still open. Studio "
                    "waits for their reports now (the wait_handoffs call below). Check them against your answer. If "
                    "something must still be fixed you may delegate it; otherwise reply with your final message to "
                    "the requester again, complete and updated with these results (plain text).")
            wait = {"id": f"call_studio_wait_{self.task_id}_{round_}", "name": "wait_handoffs", "arguments": {},
                    "raw_arguments": "{}"}
            again = agent.run(note, session=self.lead_session, initial_calls=[wait])
            result = _combined(result, again)
        return result

    def _notify_running(self, author: str, changes) -> None:
        """Tell teammates still working when files they read or produce were just published."""
        if not changes:
            return
        paths = [c.path for c in changes]
        with self.lock:
            running = [c for c in self.claims if c.started and not c.done.is_set() and author not in c.members]
        for claim in running:
            hit = [p for p in paths if any(_overlaps(p, w) for w in claim.writes + claim.reads)]
            if not hit:
                continue
            for name in sorted(claim.members):
                self.channel.deliver(name, f"[Studio] {self.group.member(author).name} ({author}) just published "
                                     f"files you read or produce: {', '.join(hit[:20])}. They are in the team "
                                     "project, not in your workspace copy; say in your report if they change your work.")

    def _plan_result_tool(self, lead_id: str) -> Tool:
        def record_result(_ctx, args):
            keys = list(dict.fromkeys(args["step_ids"]))
            rows = {s["id"]: s for s in (self.plan or {}).get("steps", [])}
            summary = args["result"].strip()
            if not summary or not keys or any(k not in rows for k in keys):
                return ToolResult.failure("Provide a result and existing step IDs.")
            decision = args.get("decision")
            if decision:
                if decision not in ("accept", "rework"):
                    return ToolResult.failure("Choose accept or rework for a decision.")
                kind, status = "plan_decision", "completed" if decision == "accept" else "rework"
            else:
                if any(rows[k].get("owner") not in (None, lead_id) for k in keys):
                    return ToolResult.failure("Record your own work on lead-owned or unassigned steps.")
                kind, status = "plan_work", "completed"
            self.store.append(kind, task_id=self.task_id, agent=lead_id, step_ids=keys, text=summary, status=status)
            return ToolResult(ok=True, content="Plan result recorded for " + ", ".join(keys) + ".")

        return Tool(name="record_plan_result",
                    description="Report your own non-file work, or explicitly accept or return selected steps. Omit decision to record work without signing it.",
                    parameters={"type": "object", "properties": {
                        "step_ids": {**PLAN_STEP_IDS, "minItems": 1},
                        "result": {"type": "string", "minLength": 1, "maxLength": 1200},
                        "decision": {"type": "string", "enum": ["accept", "rework"]}},
                        "required": ["step_ids", "result"]}, fn=record_result)

    def _plan_tool(self, lead_id: str) -> Tool:
        linked = self.settings.team.plan_links
        return Tool(
            name="update_plan",
            description=("Replace the task plan the requester sees. List every step with its status; update it when "
                         "you start or finish a step or change the approach." if not linked else
                         "Replace the task plan. Use step IDs to keep assignment evidence when renaming or reordering. "
                         "Set completed to accept a step, or rework to return it."),
            parameters={"type": "object", "properties": {
                "explanation": {"type": "string", "description": "Optional short note on what changed"},
                "steps": {"type": "array", "minItems": 1, "maxItems": 20, "items": {
                    "type": "object", "properties": {
                        **({"id": {"type": "string", "description": "Stable step ID. Assigned when omitted."},
                            "lead_outputs": {"type":"array","items":{"type":"string"},"maxItems":40,
                                             "description":"For your own (lead-owned) steps only: exact project paths whose publication completes this step (teammate files go in delegate_task deliverables)"},
                            "on_delivery": {"type":"boolean","description":"For your own (lead-owned) steps only: your final answer itself delivers this step"}}
                           if linked else {}),
                        "step": {"type": "string"},
                        "status": {"type": "string", "enum": ["pending", "in_progress", "completed"] + (["rework"] if linked else [])},
                        "owner": {"type": "string", "description": "Teammate id responsible, if any"},
                        "after": {"type": "array",
                                  "items": {"anyOf": [{"type": "integer", "minimum": 1}, {"type": "string"}]},
                                  "description": ("Earlier steps this step needs first: their IDs"
                                                  + (" or numbers (1-based)" if linked else " (1-based numbers)"))}},
                    "required": ["step", "status"]}},
            }, "required": ["steps"]},
            fn=lambda _ctx, args: self._update_plan(lead_id, args))

    # -- delegation ---------------------------------------------------------
    def _claim_for(self, name: str, args: dict) -> Claim:
        """The claim a hand-off call holds: its teammates, the files it writes and the files it reads."""
        if name == "review_loop":
            members = [str(args.get(k, "")).strip() for k in ("author", "reviewer")]
            return Claim(" and ".join(members), members, args.get("deliverables") or [], [])
        assignee = str(args.get("assignee", "")).strip()
        return Claim(assignee, [assignee], args.get("deliverables") or [], args.get("inputs") or [],
                     background=self._wants_background(args))

    def _register(self, claim: Claim) -> Claim:
        with self.lock:
            self.claims.append(claim)
        return claim

    def _start_together(self, calls: list[tuple[str, dict]]) -> bool:
        """The hand-offs of one response start together; their claims are registered now, in response order."""
        with self.lock:
            for name, args in calls:
                claim = self._claim_for(name, args)
                claim.taken = False
                self.announced.setdefault(_claim_args(name, args), []).append(self._register(claim))
        return True

    def _take_claim(self, name: str, args: dict) -> Claim:
        with self.lock:
            queued = self.announced.get(_claim_args(name, args))
            if queued:
                claim = queued.pop(0)
                claim.taken = True
                return claim
        return self._register(self._claim_for(name, args))

    def _drop_announced(self, claim: Claim) -> None:
        with self.lock:
            for queue in self.announced.values():
                if claim in queue:
                    queue.remove(claim)
        self._release(claim)

    def _release(self, claim: Claim | None) -> None:
        if claim is None:
            return
        claim.done.set()
        with self.lock:
            if claim in self.claims:
                self.claims.remove(claim)

    def _wait_turn(self, claim: Claim) -> list[str]:
        """Wait for every earlier, unfinished hand-off this one conflicts with; returns why it waited."""
        with self.lock:
            position = self.claims.index(claim) if claim in self.claims else len(self.claims)
            blockers = [(other, why) for other in self.claims[:position]
                        if not other.done.is_set() and (why := other.conflict(claim))]
            if blockers:
                self.waiting_turns += 1
        waited = []
        try:
            for other, (reason, subject) in blockers:
                began, waits = time.monotonic(), not other.done.is_set()
                if waits:
                    self._waiting(claim, other, reason, subject)
                while not other.done.wait(0.5):
                    self.control.boundary()
                    if self.should_cancel():
                        return waited
                    if not other.taken and time.monotonic() - other.created > ANNOUNCE_GRACE:
                        self._drop_announced(other)  # its call never ran (refused before dispatch or interrupted)
                        break
                else:
                    if waits:
                        waited.append(f"Waited for {other.label} to finish before starting: "
                                      f"{other.why(reason, subject)}.")
                if waits:
                    self.store.trace(self.task_id, {"task_id": self.task_id, "type": "handoff_wait_end",
                                                    "agent": self.primary, "assignees": sorted(claim.members),
                                                    "waiting_for": sorted(other.members),
                                                    "seconds": round(time.monotonic() - began, 1)})
                # A hand-off stopped by a pause also ends its claim; this one pauses too instead of starting.
                self.control.boundary()
        finally:
            if blockers:
                with self.lock:
                    self.waiting_turns -= 1
        claim.started = True
        return waited

    def _waiting(self, claim: Claim, other: Claim, reason: str, subject: str) -> None:
        """Show in the chat and the trace that a hand-off waits, for whom and why (Codex CollabWaitingBegin)."""
        fields = {"assignees": sorted(claim.members), "waiting_for": sorted(other.members), "reason": reason,
                  "path": subject if reason != "member" else ""}
        self.store.append("handoff_waiting", task_id=self.task_id, agent=self.primary, **fields)
        self.store.trace(self.task_id, {"task_id": self.task_id, "type": "handoff_wait", "agent": self.primary,
                                        **fields})

    def _delegate(self, lead_id: str, args: dict) -> ToolResult:
        claim = self._take_claim("delegate_task", args)
        try:
            return self._delegate_one(lead_id, args, claim)
        finally:
            if not claim.in_thread:  # a background hand-off releases its claim when it reports
                self._release(claim)

    def _review(self, lead_id: str, args: dict) -> ToolResult:
        claim = self._take_claim("review_loop", args)
        try:
            waited = self._wait_turn(claim)
            result = self._review_loop(lead_id, args)
            return result if not waited else ToolResult(ok=result.ok, content="\n".join(waited + [result.content]))
        finally:
            self._release(claim)

    def _infer_step_link(self, deliverables: list[str], valid_ids: set[str], *, review: bool = False) -> tuple[dict, str]:
        """When the lead omits `step`/`step_ids` on a follow-up that rewrites an existing step's deliverable,
        inherit that step ID if and only if exact normalized paths point unambiguously to one current plan step."""
        if review or not (self.plan and self.plan.get("linked")) or not valid_ids or not deliverables:
            return {}, ""
        wanted = {_norm(p) for p in deliverables if _norm(p)}
        if not wanted:
            return {}, ""
        events = [e for e in self.store.chat(self.trigger_seq - 1) if e.get("task_id") == self.task_id]
        added: dict[int, list[str]] = {}
        by_seq = {e["seq"]: e for e in events}
        for e in events:
            if e.get("kind") == "plan_evidence":
                for item in e.get("links", []):
                    step_ids = [s for s in item.get("step_ids", []) if s in valid_ids]
                    if not step_ids:
                        continue
                    added.setdefault(item["source_seq"], []).extend(step_ids)
                    source = by_seq.get(item["source_seq"], {})
                    if source.get("kind") == "report" and source.get("assignment_seq"):
                        added.setdefault(source["assignment_seq"], []).extend(step_ids)
        delegation_steps: dict[int, list[str]] = {}
        step_files: dict[str, set[str]] = {}
        for e in events:
            ekind = e.get("kind")
            if ekind == "delegation":
                steps = [s for s in dict.fromkeys(evidence_steps(e) + added.get(e["seq"], [])) if s in valid_ids]
                delegation_steps[e["seq"]] = steps
                if e.get("review"):
                    continue
                for s in steps:
                    step_files.setdefault(s, set()).update(_norm(p) for p in e.get("deliverables") or [] if _norm(p))
            elif ekind == "report":
                if e.get("review"):
                    continue
                original = e.get("assignment_seq")
                steps = [s for s in dict.fromkeys(
                    evidence_steps(e) + added.get(e["seq"], []) + delegation_steps.get(original, [])
                ) if s in valid_ids]
                for s in steps:
                    step_files.setdefault(s, set()).update(
                        _norm(c.get("path", "")) for c in e.get("changes") or []
                        if c.get("change") != "deleted" and _norm(c.get("path", ""))
                    )
        matched = {s for s, files in step_files.items() if files & wanted}
        if len(matched) != 1:
            return {}, ""
        step_id = next(iter(matched))
        overlap = sorted(step_files[step_id] & wanted)
        note = (f"Plan: automatically linked this follow-up to step '{step_id}' via overlapping deliverables "
                f"({', '.join(overlap[:5])}); pass `step` explicitly on future delegations.")
        return {"step": step_id, "inferred_step": True}, note

    def _step_link(self, requested, extra=(), *, deliverables=(), review: bool = False):
        if not (self.settings.team.plan_links and self.settings.team.plans):
            return {}, ""
        ids = [s["id"] for s in (self.plan or {}).get("steps", []) if s.get("id")]
        requested_ids = list(dict.fromkeys(([requested] if requested else []) + list(extra)))
        if not requested_ids:
            return self._infer_step_link(list(deliverables), set(ids), review=review)
        valid = [key for key in requested_ids if key in ids]
        invalid = [key for key in requested_ids if key not in ids]
        link = {"step": valid[0]} if valid else {}
        if len(valid) > 1:
            link["step_ids"] = valid
        if invalid:
            link["requested_step"] = invalid[0]
            return link, f"Unknown plan steps: {', '.join(invalid)}. Valid step IDs: {', '.join(ids) or '(no plan yet)'}."
        return link, ""

    def _delegate_one(self, lead_id: str, args: dict, claim: Claim) -> ToolResult:
        assignee = str(args.get("assignee", "")).strip()
        task = str(args.get("task", "")).strip()
        deliverables = [str(p).strip() for p in args.get("deliverables") or [] if str(p).strip()][:40]
        if len(task) < 10:
            return ToolResult.failure("The assignment is empty or too short. Describe the work completely.")
        fresh, review = args.get("fresh", False), args.get("review", False)
        if not isinstance(fresh, bool) or not isinstance(review, bool):
            return ToolResult.failure("fresh and review must be true or false")
        kind = args.get("kind") if self.settings.team.handoff_kinds else None
        if kind is not None and kind not in KIND_PROFILES:
            return ToolResult.failure(f"kind must be one of: {', '.join(HANDOFF_KINDS)}")
        if kind is None and review and self.settings.team.handoff_kinds:
            kind = "check"  # review: true is the older spelling of a check hand-off
        elif kind is not None:
            review = kind == "check"
        background = claim.background
        try:
            checks = validate(list(args.get("checks") or []))
        except ValueError as exc:
            return ToolResult.failure(f"Invalid checks: {exc}")
        inputs = [str(p).strip() for p in args.get("inputs") or [] if str(p).strip()][:40]
        reference_files = [str(p).strip() for p in args.get("reference_files") or [] if str(p).strip()][:40]
        missing = []
        # The lead's files go to the project first: inputs it just wrote are present, and the plan sees its outputs.
        self._publish_lead(lead_id)
        waited = self._wait_turn(claim)
        if inputs:
            manifest = self.store.project.manifest()
            missing = [p for p in inputs if not _present(p, manifest)]
            task += "\n\nRead these project files first: " + ", ".join(inputs)
            if missing:
                task += ("\nNot in the project yet: " + ", ".join(missing)
                         + ". Say in your report if the work needed them.")
        link, warning = self._step_link(args.get("step"), args.get("step_ids", []),
                                        deliverables=deliverables, review=review)
        self._link_lead_prereq_publish(lead_id, link, inputs)
        unclosed = self._unclosed_lead_steps(lead_id, link)
        spec = {k: v for k, v in (("kind", kind), ("inputs", inputs or None),
                                  ("reference_files", reference_files or None)) if v}
        if background:
            spec["background"] = True
        outcome = self._assign(lead_id, assignee, task, deliverables=deliverables, max_steps=args.get("max_steps"),
                               fresh=fresh, checks=checks, review=review, link=link, spec=spec, claim=claim)
        if isinstance(outcome, ToolResult):
            return outcome
        if "started" in outcome:
            member = self.group.member(assignee)
            lines = [f"Started in the background: {member.name} ({assignee}) is working on this"
                     + (f" ({kind} hand-off)." if kind else ".")
                     + " The report arrives as a message at your next step after they finish; call wait_handoffs "
                       "when your next step needs it. Until then, do not edit, assign or build on the files it produces."]
            if warning:
                lines.insert(0, warning)
            lines[1:1] = waited
            if missing:
                lines.append("Inputs that were not in the project when the assignment started: " + ", ".join(missing) + ".")
            if unclosed:
                lines.append(unclosed)
            lines.append(f"Delegations used: {self.delegations}/{self.settings.team.max_delegations}.")
            return ToolResult(ok=True, content="\n".join(lines))
        lines = waited + self._report_lines(outcome)
        if warning:
            lines.insert(0, warning)
        if missing:
            lines.append("Inputs that were not in the project when the assignment started: " + ", ".join(missing)
                         + ". If the work depended on them, run it again after the step that produces them.")
        if unclosed:
            lines.append(unclosed)
        if ready := self._ready_note():
            lines.append(ready)
        lines.append(f"Delegations used: {self.delegations}/{self.settings.team.max_delegations}.")
        ok = outcome["result"].status in {"completed", "max_steps", "stuck", "budget"}
        return ToolResult(ok=ok, content="\n".join(lines))

    def _assign(self, lead_id: str, assignee: str, task: str, *, deliverables: list[str], max_steps=None,
                fresh: bool = False, checks: list[dict] | None = None, review: bool = False,
                loop: dict | None = None, link: dict | None = None,
                spec: dict | None = None, claim: Claim | None = None) -> dict | ToolResult:
        """Run one member assignment; returns its outcome, or a refusal for the lead.

        ``spec`` carries the hand-off kind, its inputs and whether it runs in the background; a background
        hand-off returns ``{"started": entry}`` at once and reports through the lead's inbox.
        """
        valid = self._member_ids(lead_id)
        if assignee not in valid:
            return ToolResult.failure(f"Unknown assignee {assignee!r}. Choose one of: {', '.join(valid)}.")
        with self.lock:
            entry = self.inflight.get(assignee)
        if entry is not None and entry.get("background"):
            return ToolResult.failure(f"{assignee} is still working on a hand-off in the background. Call "
                                      "wait_handoffs for that report first, or choose another teammate.")
        spec = dict(spec or {})
        if entry is None:
            if spec.get("background"):
                spec["run"] = {"task": task, "deliverables": deliverables, "max_steps": max_steps, "fresh": fresh,
                               "checks": checks or [], "review": review}
            entry = self._open_assignment(lead_id, assignee, task, deliverables, max_steps, checks, review, loop, link,
                                          spec)
            if isinstance(entry, ToolResult):
                return entry
            if spec.get("background"):
                if claim is not None:
                    claim.in_thread = True  # the background run releases the claim when it reports
                self._start_background(entry, claim)
                return {"started": entry}
        member = self.group.member(assignee)
        result, changes, skipped = self._run_member(member, task, deliverables, entry, max_steps,
                                                    fresh=fresh, review=review)
        with self.lock:
            self.inflight.pop(assignee, None)
            self.lead_baseline = self.store.project.checkout(self.store.workspace(lead_id))
        return self._close_assignment(member, task, deliverables, result, changes, skipped, checks, review, loop,
                                      entry.get("browser_checks"), entry)

    def _known(self, member_id: str) -> bool:
        return any(m.id == member_id for m in self.group.members)

    def _publish_lead(self, lead_id: str) -> None:
        """Publish the lead's own edits to the team project; a second call publishes nothing new."""
        with self.lock:
            lead_workspace = self.store.workspace(lead_id)
            own, _ = self.store.project.publish(lead_workspace, self.lead_baseline, author=lead_id,
                                                task_id=self.task_id, note="lead edits before delegation",
                                                conflicts_dir=self.store.task_dir(self.task_id) / "conflicts")
            if own:
                self.store.append("publish", task_id=self.task_id, agent=lead_id, changes=[c.as_dict() for c in own])
                self.lead_baseline = self.store.project.checkout(lead_workspace)

    def _open_assignment(self, lead_id, assignee, task, deliverables, max_steps, checks, review, loop, link, spec=None):
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
            self._publish_lead(lead_id)
        spec = spec or {}
        extra = {k: v for k, v in (("review", review or None), ("loop", loop), ("checks", checks or None),
                                   ("handoff_kind", spec.get("kind")), ("background", spec.get("background")),
                                   ("inputs", spec.get("inputs")),
                                   ("reference_files", spec.get("reference_files"))) if v}
        event = self.store.append("delegation", task_id=self.task_id, agent=lead_id, assignee=assignee,
                                  task=task, deliverables=deliverables, **extra, **(link or {}))
        entry = {"assignee": assignee, "seq": event["seq"], **(link or {}),
                 **{k: v for k, v in spec.items() if k in ("kind", "inputs", "reference_files", "background", "run", "evidence")}}
        with self.lock:
            self.inflight[assignee] = entry
        return entry

    def _close_assignment(self, member, task, deliverables, result, changes, skipped, checks, review, loop, browser_checks=None, entry=None):
        browser_checks = browser_checks if browser_checks and browser_checks["findings"] else None
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
        report_extra = {k: v for k, v in (("checks", checked), ("loop", loop), ("review", review or None),
                                          ("browser_checks", browser_checks)) if v}
        if self.talk and entry:
            report_extra.update(assignment_seq=entry["seq"], talk=dict(entry.get("talk", {})))
        if self.settings.team.plan_links and entry:
            report_extra.update(assignment_seq=entry["seq"])
            report_extra.update({k: entry[k] for k in ("step", "step_ids", "requested_step") if k in entry})
            if missing:
                report_extra["missing_deliverables"] = missing
            if skipped:
                report_extra["unpublished_files"] = list(skipped)
            if review:
                report_extra["verdict"] = review_verdict(report)
        if entry:
            if entry.get("kind"):  # "kind" is the chat event's own type
                report_extra["handoff_kind"] = entry["kind"]
            if entry.get("background"):
                report_extra["background"] = True
            if entry.get("runtime"):
                report_extra.update(entry["runtime"])
        with self.lock:
            self.evidence.append(handoffs.evidence_row(
                seq=entry["seq"] if entry else 0, member=assignee, name=member.name,
                kind=(entry or {}).get("kind") or ("check" if review else ""), status=result.status, report=report,
                changes=changes, checked=checked, browser=browser_handoff(browser_checks) if browser_checks else "",
                verdict=review_verdict(report) if review else ""))
            del self.evidence[:-handoffs.EVIDENCE_KEPT]
        self._notify_running(assignee, changes)
        self.store.append("report", task_id=self.task_id, agent=assignee, status=result.status, text=report,
                          changes=[c.as_dict() for c in changes], steps=result.steps, tool_calls=result.tool_calls,
                          **report_extra)
        return {"member": member, "result": result, "report": report, "changes": changes, "skipped": skipped,
                "missing": missing, "checks": checked, "browser_checks": browser_checks}

    def _report_lines(self, outcome: dict) -> list[str]:
        member, result, changes = outcome["member"], outcome["result"], outcome["changes"]
        lines = [f"Report from {member.name} ({member.id}) — status: {result.status}"
                 + ("" if result.status == "completed" else " (the assignment did not finish normally)"),
                 "", _clip(outcome["report"], REPORT_LIMIT), ""]
        if outcome.get("browser_checks"):
            lines.append(browser_handoff(outcome["browser_checks"]))
        if self.settings.team.resume_members:
            lines.append(f"Session: {'continued their earlier session in this task' if result.resumed else 'new session'}.")
        if changes:
            lines.append("Published to the team project: " + ", ".join(
                f"{c.path} ({c.change}{', overwrote a concurrent change' + (f' [saved copy: {c.conflict_copy}]' if c.conflict_copy else '') if c.conflict else ''})"
                for c in changes))
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
        if self.settings.team.fast and type(rounds_max) is int:
            rounds_max = min(rounds_max, 2)
        if type(rounds_max) is not int or not 1 <= rounds_max <= 5:
            return ToolResult.failure("max_rounds must be between 1 and 5")
        try:
            checks = validate(list(args.get("checks") or []))
        except ValueError as exc:
            return ToolResult.failure(f"Invalid checks: {exc}")
        names = {m.id: m.name for m in self.group.members}
        step_ids = args.get("step_ids", [])
        link, warning = self._step_link(args.get("step"), step_ids)
        retest_link, retest_warning = self._step_link(args.get("step"), step_ids + args.get("retest_steps", []))
        warning = retest_warning or warning
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
                                            loop={"id": state["id"], "round": number, "role": "review"},
                                            link=retest_link if number > 1 else link,
                                            spec={"kind": "check"} if self.settings.team.handoff_kinds else None)
                    if isinstance(reviewed, ToolResult):
                        state["stop"] = reviewed.content
                        break
                    state["review_browser_checks"] = reviewed.get("browser_checks")
                    state["last_review"] = reviewed["report"]
                    state["verdict"] = review_verdict(reviewed["report"])
                    state["rounds"].append(f"Round {number} — review by {names[reviewer]}: {state['verdict'].upper()}"
                                           + (f" ({reviewed['result'].status})" if reviewed["result"].status != "completed" else ""))
                    if (state["verdict"] != "changes requested" or reviewed["result"].status in ("error", "cancelled")
                            or number == rounds_max):
                        break
                    state["findings"] = VERDICT_LINE.sub("", reviewed["report"], count=1).strip() or reviewed["report"]
                    state["phase"] = "fix"
                fix_task = (f"Review round {number}: {names[reviewer]} requested changes. Fix these findings in the "
                            f"project, verify your fix, then report what you changed and how you verified it.\n\n"
                            f"{state['findings']}")
                fixed = self._assign(lead_id, author, fix_task, deliverables=deliverables, checks=checks,
                                     loop={"id": state["id"], "round": number, "role": "fix"}, link=link,
                                     # the findings are in the brief already
                                     spec={"kind": "fix", "evidence": False} if self.settings.team.handoff_kinds else None)
                if isinstance(fixed, ToolResult):
                    state["stop"] = fixed.content
                    break
                state["fix_browser_checks"] = fixed.get("browser_checks")
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
        if warning:
            lines.insert(0, warning)
        if stop:
            lines.append("Stopped early: " + stop)
        if last_review:
            lines += ["", f"Last review from {names[reviewer]}:", _clip(last_review, 6000)]
        if state.get("review_browser_checks"):
            lines.append(browser_handoff(state["review_browser_checks"]))
        if last_fix:
            lines += ["", f"Last report from {names[author]}:", _clip(last_fix, 3000)]
        if state.get("fix_browser_checks"):
            lines.append(browser_handoff(state["fix_browser_checks"]))
        if touched:
            lines.append("Files changed by the fixes: " + ", ".join(f"{p} ({c})" for p, c in sorted(touched.items())))
        if last_checks:
            lines += check_lines(last_checks)
        if ready := self._ready_note():
            lines.append(ready)
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
            if self.settings.team.plan_links and "id" in item:
                row["id"] = item["id"]
            if item.get("owner"):
                row["owner"] = str(item["owner"]).strip()[:60]
            if self.settings.team.plan_links and (item.get("lead_outputs") or item.get("on_delivery")):
                if row.setdefault("owner", lead_id) != lead_id:
                    return ToolResult.failure("lead_outputs and on_delivery belong to lead-owned steps only; declare teammate files in delegate_task deliverables.")
                if lead_outputs := item.get("lead_outputs"):
                    if any(not p.strip() or p.startswith("/") or any(part in ("", ".", "..") for part in p.split("/")) for p in lead_outputs):
                        return ToolResult.failure("Use exact project-relative output paths.")
                    row["lead_outputs"] = list(dict.fromkeys(lead_outputs))
                if item.get("on_delivery"):
                    row["on_delivery"] = True
            after = item.get("after") or []
            if after:
                if not isinstance(after, list):
                    return ToolResult.failure(f"Step {len(steps) + 1}: `after` is a list of earlier steps.")
                row["after"] = after  # resolved to step numbers below, once every step has its ID
            elif "after" not in item:
                row["_keep_after"] = True  # omitted (not []): keep this step's dependencies from the previous plan
            steps.append(row)
        if not 1 <= len(steps) <= 20:
            return ToolResult.failure("A plan has 1–20 steps.")
        explanation = str(args.get("explanation") or "").strip()[:600]
        with self.lock:
            meta = {}
            if self.settings.team.plan_links:
                try:
                    meta = {"used_ids": identify_steps(steps, self.plan), "linked": True}
                except ValueError as exc:
                    return ToolResult.failure(str(exc))
            kept = _inherit_after(steps, self.plan) if self.settings.team.plan_links else 0
            for row in steps:
                row.pop("_keep_after", None)
            problem = _resolve_after(steps)
            if problem:
                return ToolResult.failure(problem)
            already_done = set()
            if self.settings.team.plan_links and self.plan and self.plan.get("linked"):
                prior_view = plan_view(self.record, self.store.chat(self.trigger_seq - 1))
                if prior_view:
                    already_done = {r["id"] for r in prior_view["steps"] if r.get("completed") or r.get("stage") == "completed"}
            for row in steps:
                if row["status"] == "in_progress" and row.get("after"):
                    if any(steps[k - 1]["status"] != "completed" and steps[k - 1].get("id") not in already_done
                           for k in row["after"] if 1 <= k <= len(steps)):
                        row["status"] = "pending"
            self.plan = {"steps": steps, "explanation": explanation, "updated_at": now(), **meta}
            self._update(plan=self.plan)
        self.store.append("plan", task_id=self.task_id, agent=lead_id, steps=steps, explanation=explanation,
                          **({"linked": True} if self.settings.team.plan_links else {}))
        visible = (plan_view(self.record, self.store.chat(self.trigger_seq - 1))["steps"]
                   if self.settings.team.plan_links else steps)
        stages = [row.get("stage", row["status"]) for row in visible]
        done = stages.count("completed")
        ready = [str(i + 1) for i, row in enumerate(visible) if stages[i] == "pending"
                 and all(stages[n - 1] == "completed" for n in row.get("after", []))]
        hint = f" Ready to start: steps {', '.join(ready)}." if len(ready) > 1 else ""
        ids = (" Steps: " + ", ".join(f"{s['id']} {s['step']}" + (f" (lead_outputs: {', '.join(s['lead_outputs'])})" if s.get("lead_outputs") else "")
                                      for s in steps) if self.settings.team.plan_links else "")
        kept_note = (f" Kept the dependencies of {kept} step(s) that omitted `after` (pass `after: []` to clear them)."
                     if kept else "")
        return ToolResult(ok=True, content=f"Plan updated: {done}/{len(steps)} steps done.{hint}{kept_note}{ids} "
                                           "The requester can see it.")

    def _run_member(self, member: Member, task: str, deliverables: list[str], entry: dict, max_steps=None,
                    *, fresh: bool = False, review: bool = False, reconcile: bool = False):
        trigger_seq = entry["seq"]
        workspace = self.store.workspace(member.id)
        if "baseline" not in entry:
            entry["baseline"] = self.store.project.checkout(workspace)
        baseline = entry["baseline"]
        rejoining = member.id in self.control.frames
        kind = entry.get("kind") if self.settings.team.handoff_kinds else None
        # Reviews keep the lead's thinking (or the reviewer settings); in fast mode members' own work does not think.
        prompt_settings = self.settings.reviewer() if review else self.settings.member()
        if kind:
            settings = kind_settings(self.settings, kind, keep_thinking=self.thinking_pinned,
                                     keep_effort=self.effort_pinned)
            provider = self._provider_for(settings, review)
            if max_steps is None:
                max_steps = min(KIND_PROFILES[kind].max_steps, settings.limits.max_steps)
        else:
            settings = prompt_settings
            if review:
                provider = self.reviewer_provider if self.reviewer_provider is not None else self.provider
            else:
                provider = self.member_provider if self.member_provider is not None else self.provider
        # Review work may use another model or thinking mode, so it keeps a session of its own; adapt works
        # without thinking and without a browser, and a conversation keeps one thinking mode and tool list.
        key = (member.id, "review" if review else "adapt" if kind == "adapt" else "work")
        session = None
        if self.settings.team.resume_members or self.talk:
            with self.lock:
                session = self.sessions.get(key) if self.settings.team.resume_members or rejoining else None
                if session is None or (fresh and not rejoining):
                    session = self.sessions[key] = AgentSession()
        continuing = (not rejoining and session is not None and session.resumable and key in self.session_marks)
        lead = self.group.member(self.primary)
        assignment = task
        if deliverables:
            assignment += "\n\nFiles you are expected to create or update: " + ", ".join(deliverables)
        limits = settings.limits if max_steps is None else replace(settings.limits, max_steps=max_steps)
        if kind:
            assignment += f"\n\n{handoffs.KIND_NOTES[kind]} Step budget for this hand-off: {limits.max_steps} steps."
        elif review:
            assignment += ("\n\nThis assignment is a review or test: you own the verification of this work. Check "
                           "the real behaviour end to end and report concrete evidence.")
        if not reconcile and not rejoining:
            preload_paths = list(dict.fromkeys(list(entry.get("inputs") or []) + list(entry.get("reference_files") or [])))
            if preload_paths and self.settings.team.preload_inputs:
                block = handoffs.preload(workspace, preload_paths)
                if block:
                    assignment += "\n\n" + block
            if kind and self.settings.team.handoff_evidence and entry.get("evidence", True):
                with self.lock:
                    rows = [row for row in self.evidence if row["seq"] < trigger_seq]
                block = handoffs.evidence_block(rows, kind=kind, member=member.id, continuing=continuing,
                                                paths=preload_paths + list(deliverables))
                if block:
                    assignment += "\n\n" + block
        entry["runtime"] = {
            "model": settings.model,
            "thinking": settings.thinking,
            "reasoning_effort": settings.reasoning_effort if settings.thinking == "enabled" else None,
        }
        assignment = f"Assignment from {lead.name} ({lead.id}):\n\n" + assignment
        registry = self._registry(member.id, lead=False)
        if kind and not KIND_PROFILES[kind].browser:
            registry.remove("run_browser")
        agent = Agent(member.id, provider, registry, Sandbox(workspace, member.id),
                      settings, limits=self._time_boxed(limits, reserve=45),
                      # The system prompt stays the same for every kind, so a continued session keeps its cache.
                      system_prompt=member_prompt(member, lead, self.group.members, prompt_settings, self.interactive),
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
        agent.report_requested = lambda: entry.get("report_request")
        if self.talk:
            self.talk.start(member.id, entry, review)
        try:
            result = agent.run(assignment, context=context, session=session)
            # A failed model request should not cost the member its work: continue the same session.
            while (result.status == "error" and result.error.startswith("model request failed")
                   and self.settings.team.resume_members and session is not None and session.resumable and recovered < MODEL_ERROR_RETRIES
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
            if self.talk:
                self.talk.finish(member.id, entry)
            with self.lock:
                self.active.remove(member.id)
                self._update(active=list(self.active))
        changes, skipped = self.store.project.publish(workspace, baseline, author=member.id, task_id=self.task_id,
                                                      note=task[:300],
                                                      conflicts_dir=self.store.task_dir(self.task_id) / "conflicts")
        with self.lock:
            if session is not None:
                self.session_marks[key] = {"manifest": self.store.project.manifest(), "seq": trigger_seq}
            metrics = _metrics(member.id, result)
            if self.talk:
                metrics.update(assignment_seq=entry["seq"], talk=dict(entry.get("talk", {})))
            if review:
                metrics["review"] = True
            if kind:
                metrics["kind"] = kind
            if entry.get("background"):
                metrics["background"] = True
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
        self.meter.restore(self.control.saved.get("meter"))
        self.meter.started = time.time() - self.control.saved.get("elapsed", 0)
        self.meter.budget_minutes = float(self.options.get("budget_minutes")
                                          or (FAST_SOFT_MINUTES if self.settings.team.fast else 0))
        if self.settings.team.task_seconds:
            self.deadline = time.monotonic() + self.settings.team.task_seconds - self.control.saved.get("elapsed", 0)
        self.meter.budget_tokens = int(self.options.get("budget_tokens") or 0)
        me = self.group.member(self.primary)
        can_delegate = self.primary == self.group.lead and len(self.group.members) > 1
        self.can_delegate = can_delegate
        workspace = self.store.workspace(self.primary)
        saved = self.control.saved
        if saved:
            self.record = self.store.load_task(self.task_id)
            self.lead_baseline = saved["lead_baseline"]
            self.consults.calls = saved.get("consult_calls", 0)
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
            self.bg_finished = list(saved.get("bg_finished") or [])
            self.bg_untaken = saved.get("bg_untaken", 0)
            self.evidence = list(saved.get("evidence") or [])
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
                              **({"inherited_plan":True} if self.plan and self.plan.get("linked") else {}),
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
        keep_session = (settings.team.mailboxes and settings.team.lead_consult) or settings.team.background_handoffs
        try:
            for entry in list(self.inflight.values()):
                if entry.get("background"):  # resumed task: background hand-offs continue from their frames
                    self._start_background(entry)
            result = agent.run(assignment, context=self._context(self.primary, self.trigger_seq),
                               initial_calls=self.initial_calls, session=self.lead_session if keep_session else None)
            result = self._after_background(agent, result)
        except PauseRequested:
            self._join_background()  # each background member saves its frame before the checkpoint is written
            raise
        self._join_background(timeout=60)  # after a cancel, members stop at their next boundary
        self.consults.close()
        self.channel.finish()
        changes, _ = self.store.project.publish(workspace, self.lead_baseline, author=self.primary,
                                                task_id=self.task_id, note="final edits",
                                                conflicts_dir=self.store.task_dir(self.task_id) / "conflicts")
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
                   "background_handoffs": sum(1 for a in self.assignments if a.get("background")),
                   "handoff_kinds": {k: n for k in HANDOFF_KINDS
                                     if (n := sum(1 for a in self.assignments if a.get("kind") == k))},
                   "parallel_batches": self.parallel_batches, "parallel_reads": self.parallel_reads,
                   "time": {k: v for k, v in self.meter.snapshot().items() if k != "alerts"},
                   "steps": totals["steps"] + sum(a["steps"] for a in self.assignments),
                   "tool_calls": totals["tool_calls"] + sum(a["tool_calls"] for a in self.assignments)}
        if self.talk:
            metrics["communication_limits"] = {name: getattr(self.settings.team, name)
                                               for name in ("check_ins", "teammate_questions", "teammate_notes")}
        communication = metrics["time"]["communication"]
        metrics["prompt_tokens"] += communication["auxiliary_prompt"]
        metrics["completion_tokens"] += communication["auxiliary_output"]
        metrics["prompt_cache_hit_tokens"] += communication["auxiliary_cached"]
        self._update(status=status, finished_at=now(), final_answer=answer, error=result.error,
                     active=[], metrics=metrics, assignments=self.assignments, delegations=self.delegations)
        self.store.append("task_finished", task_id=self.task_id, status=status, metrics=metrics, error=result.error)
        return self.record


def _inherit_after(steps: list[dict], previous: dict | None) -> int:
    """A whole-plan update that omits `after` for a step keeps that step's earlier dependencies.

    Updating statuses should not erase the dependency graph (S1T1-v3.3 lost it in its closing update).
    Kept references are step IDs, so they follow reordering; ones that are gone or no longer earlier are
    dropped. Returns how many steps kept dependencies.
    """
    old = (previous or {}).get("steps", [])
    old_ids = [s.get("id") for s in old]
    by_id = {s.get("id"): s for s in old if s.get("id")}
    kept = 0
    for number, row in enumerate(steps, 1):
        if not row.get("_keep_after") or row.get("id") not in by_id:
            continue
        earlier = {s.get("id") for s in steps[:number - 1]}
        refs = [old_ids[n - 1] for n in by_id[row["id"]].get("after", []) if 1 <= n <= len(old_ids)]
        refs = [ref for ref in refs if ref in earlier]
        if refs:
            row["after"] = refs
            kept += 1
    return kept


def _resolve_after(steps: list[dict]) -> str:
    """Turn each step's `after` (earlier step IDs or 1-based numbers) into sorted step numbers.

    Returns an error for the lead, naming the valid references, or "" when every reference is an earlier step.
    """
    for number, row in enumerate(steps, 1):
        refs = row.get("after")
        if not refs:
            row.pop("after", None)
            continue
        earlier = {s["id"]: n for n, s in enumerate(steps[:number - 1], 1) if s.get("id")}
        resolved = []
        for ref in refs:
            key = ref.strip() if isinstance(ref, str) else ref
            if type(key) is int and 1 <= key < number:
                resolved.append(key)
            elif isinstance(key, str) and key in earlier:
                resolved.append(earlier[key])
            elif isinstance(key, str) and key.isdigit() and 1 <= int(key) < number:
                resolved.append(int(key))
            else:
                if number == 1:
                    return f"Step 1 ({row.get('id') or row['step'][:40]}): `after` may only name earlier steps; the first step has none."
                valid = f"numbers 1–{number - 1}" + (f" or IDs {', '.join(earlier)}" if earlier else "")
                return (f"Step {number} ({row.get('id') or row['step'][:40]}): `after` may only name earlier steps, "
                        f"and {ref!r} is not one. Use {valid}.")
        row["after"] = sorted(set(resolved))
    return ""


def _combined(first: AgentResult, second: AgentResult) -> AgentResult:
    """One assignment that needed a second run of the same session."""
    return replace(second, steps=first.steps + second.steps, tool_calls=first.tool_calls + second.tool_calls,
                   usage=first.usage.merged(second.usage), duration_s=round(first.duration_s + second.duration_s, 2),
                   compactions=first.compactions + second.compactions, resumed=first.resumed)


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
