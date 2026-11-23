"""Project a lead's plan and the work the team actually recorded onto the same steps."""
import re
from collections import defaultdict


def identify_steps(rows, previous):
    """Assign IDs on whole-plan replacement, reserving explicit IDs before matching repeated text."""
    previous = previous or {}
    old = previous.get("steps", [])
    used = set(previous.get("used_ids", ()))
    by_text = defaultdict(list)
    for step in old:
        if step.get("id"):
            used.add(step["id"])
            by_text[step["step"]].append(step["id"])
    explicit = [row["id"] for row in rows if "id" in row]
    if any(not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}", key) for key in explicit):
        raise ValueError("Step IDs must be 1–48 letters, digits, underscores or hyphens and start with a letter or digit.")
    if len(explicit) != len(set(explicit)):
        raise ValueError("Each step needs a distinct ID.")
    assigned = set(explicit)
    used.update(explicit)
    next_id = 1
    for row in rows:
        if "id" in row:
            continue
        key = next((key for key in by_text[row["step"]] if key not in assigned), None)
        if key is None:
            while f"s{next_id}" in used:
                next_id += 1
            key = f"s{next_id}"
        row["id"] = key
        assigned.add(key)
        used.add(key)
    return sorted(used)


def plan_view(record, events):
    """Fold only this task's events. Snapshots predating stable IDs remain readable as saved."""
    plan = record.get("plan") or {}
    if not plan.get("linked"):
        return None
    rows = [{**row, "evidence": {}, "history": []} for row in plan["steps"]]
    by_id = {row["id"]: row for row in rows}
    unlinked = []
    outstanding = defaultdict(set)
    for event in events:
        if event.get("task_id") != record["id"] or event["kind"] not in ("delegation", "report"):
            continue
        row = by_id.get(event.get("step"))
        if row is None:
            if event["kind"] == "delegation":
                unlinked.append({k: event[k] for k in ("seq", "assignee", "task", "step", "requested_step") if k in event})
            continue
        evidence = row["evidence"]
        entry = {"seq": event["seq"], "time": event["time"]}
        if event["kind"] == "delegation":
            entry.update(agent=event["assignee"], task=event["task"])
            evidence["assigned"] = entry
            outstanding[row["id"]].add(event["seq"])
        else:
            outstanding[row["id"]].discard(event.get("assignment_seq"))
            entry.update(agent=event["agent"], status=event["status"], files=event.get("changes", []))
            if event.get("checks"):
                entry["checks"] = {k: event["checks"][k] for k in ("passed", "total")}
            evidence["submitted"] = entry
            if event.get("review"):
                evidence["reviewed"] = {**entry, "verdict": event.get("verdict", "unclear")}
        row["history"].append({**entry, "kind": event["kind"], **({"loop": event["loop"]} if event.get("loop") else {}),
                               **({"verdict": event["verdict"]} if event.get("verdict") else {})})

    live = record.get("status") in ("running", "queued", "paused")
    counts = {"total": len(rows), "submitted": 0, "in_progress": 0, "accepted": 0}
    for row in rows:
        evidence, decision = row["evidence"], row["status"]
        submitted = evidence.get("submitted")
        active = live and bool(outstanding[row["id"]])
        if decision in ("completed", "rework"):
            stage = decision
        elif active:
            stage = "in_progress"
        elif submitted:
            stage = "submitted" if live else "unconfirmed"
        else:
            stage = decision
        row["stage"] = stage
        counts["submitted"] += bool(submitted)
        counts["in_progress"] += stage == "in_progress"
        counts["accepted"] += decision == "completed"
    return {"steps": rows, "counts": counts, "unlinked": unlinked, "explanation": plan.get("explanation", "")}
