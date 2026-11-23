"""After a task, draft lessons for the requester to approve into role knowledge.

CrewAI's ``TaskEvaluator.evaluate`` asks a model, after each task, for bullet
suggestions that improve future similar tasks and stores them for the agents.
Here the drafts are proposals: each lesson becomes a ``proposed`` knowledge
entry for one member and is used in later tasks only after the requester
approves it with one click in the chat. A failed or unparsable retrospective
never affects the task.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .team import _clip, render_event

MAX_LESSONS = 4
SYSTEM = "You review a finished multi-agent task and write the few lessons this team should remember next time."
INSTRUCTIONS = """Write at most {limit} lessons. Each must be specific and reusable in a later task with this team:
a preference the requester stated, a working convention that helped, a mistake that cost a round and how to avoid it,
or a check that caught a real problem. Leave out anything that only applies to this one task, generic advice and praise.
Address each lesson to the member who should act on it, by id. Use scope "general" only for a lesson about that role's
craft that would hold in any team; otherwise use "group".

Answer with JSON only:
{{"lessons": [{{"member": "<id>", "scope": "group", "text": "<one or two sentences>"}}]}}
Return {{"lessons": []}} when nothing is worth keeping."""


def _task_transcript(store, task_id: str, budget: int = 30_000) -> str:
    names = {m.id: m.name for m in store.group.members}
    lines, used = [], 0
    for event in store.chat():
        if event.get("task_id") != task_id:
            continue
        line = render_event(event, names)
        if not line:
            continue
        if used + len(line) > budget:
            lines.append("…(rest of the task omitted)…")
            break
        lines.append(line)
        used += len(line)
    return "\n\n".join(lines)


def prompt(store, record: dict) -> list[dict[str, Any]]:
    roster = "\n".join(f"- {m.id}: {m.name} — {_clip(m.prompt, 240)}" for m in store.group.members)
    rows = []
    for item in record.get("assignments", []):
        extra = []
        if item.get("resumed"):
            extra.append("continued session")
        if item.get("review"):
            extra.append("review")
        if item.get("checks"):
            extra.append(f"checks {item['checks']['passed']}/{item['checks']['total']}")
        rows.append(f"- {item['agent']}: {item['status']}, {item.get('steps', 0)} steps"
                    + (f" ({', '.join(extra)})" if extra else ""))
    metrics = record.get("metrics") or {}
    body = (f"Team (lead: {store.group.lead}):\n{roster}\n\n"
            f"Request:\n{_clip(record.get('text', ''), 4000)}\n\n"
            f"Outcome: {record.get('status')} after {metrics.get('duration_s', '?')} s, "
            f"{metrics.get('delegations', 0)} assignments.\n" + ("\n".join(rows) + "\n" if rows else "")
            + f"\nWhat happened in this task:\n{_task_transcript(store, record['id'])}\n\n"
            + INSTRUCTIONS.format(limit=MAX_LESSONS))
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": body}]


def parse(text: str, members: set[str]) -> list[dict]:
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return []
    lessons = []
    for item in (data.get("lessons") if isinstance(data, dict) else None) or []:
        if not isinstance(item, dict):
            continue
        member, text_ = str(item.get("member", "")).strip(), str(item.get("text", "")).strip()
        scope = "general" if item.get("scope") == "general" else "group"
        if member in members and 10 <= len(text_) <= 800:
            lessons.append({"member": member, "scope": scope, "text": text_})
    return lessons[:MAX_LESSONS]


def draft_and_propose(studio, store, record: dict, provider) -> list[dict]:
    """Ask the model for lessons, store them as proposals and announce them in the chat."""
    response = provider.chat(prompt(store, record), temperature=0.0)
    lessons = parse(response.text, {m.id for m in store.group.members})
    entries = []
    for lesson in lessons:
        general = lesson["scope"] == "general" and lesson["member"] in store.group.role_ids
        entry = studio.roles.propose(lesson["member"], lesson["text"],
                                     "general" if general else "group:" + store.group.id, "memory")
        entries.append({"role": lesson["member"], "id": entry["id"], "text": entry["text"],
                        "scope": entry["scope"]})
    if entries:
        store.append("lessons", task_id=record["id"], entries=entries)
    return entries
