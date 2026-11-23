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


def _step_decision(row, declaration, outstanding, lead):
    assigned = row["evidence"].get("assigned", {})
    if (row["status"] in ("completed", "rework")
            and declaration.get("seq", 0) >= assigned.get("seq", 0)):
        return {"stage": row["status"], "source": "lead", "agent": declaration.get("agent", lead)}

    # A review covers the work available when its assignment began. A new
    # handoff, concurrent submission or explicit reopening invalidates it.
    history = row["history"]
    if outstanding or not assigned.get("review") or len(history) < 2:
        return None
    prior, report = history[-2:]
    if (prior["seq"] != assigned["seq"] or report.get("assignment_seq") != assigned["seq"]
            or assigned["seq"] <= declaration.get("seq", 0) or report["agent"] != assigned["agent"]
            or report.get("status") != "completed"):
        return None
    verdict = report.get("verdict")
    checks = report.get("checks", {})
    if verdict == "approved" and (not checks or checks["passed"] == checks["total"]):
        return {"stage": "completed", "source": "review", "agent": report["agent"]}
    if verdict == "changes requested":
        return {"stage": "rework", "source": "review", "agent": report["agent"]}
    return None


def plan_view(record, events):
    """Fold only this task's events. Snapshots predating stable IDs remain readable as saved."""
    plan = record.get("plan") or {}
    if not plan.get("linked"):
        return None
    rows = [{**row, "evidence": {}, "history": []} for row in plan["steps"]]
    by_id = {row["id"]: row for row in rows}
    unlinked = []
    outstanding = defaultdict(set)
    # update_plan replaces the entire list. Repeating an unchanged status for
    # another step is not a new decision on this one.
    declarations = {}
    for event in events:
        if event.get("task_id") != record["id"]:
            continue
        if event["kind"] == "plan":
            for step in event["steps"]:
                key = step.get("id")
                if key in by_id and declarations.get(key, {}).get("status") != step["status"]:
                    declarations[key] = {"status": step["status"], "seq": event["seq"], "agent": event["agent"]}
            continue
        if event["kind"] not in ("delegation", "report"):
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
            if event.get("review"):
                entry["review"] = True
            evidence["assigned"] = entry
            outstanding[row["id"]].add(event["seq"])
        else:
            outstanding[row["id"]].discard(event.get("assignment_seq"))
            entry.update(agent=event["agent"], status=event["status"], files=event.get("changes", []),
                         assignment_seq=event.get("assignment_seq"))
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
        pending = outstanding[row["id"]]
        resolution = _step_decision(row, declarations.get(row["id"], {}), pending, record.get("primary"))
        if resolution:
            stage = resolution["stage"]
            row["decision"] = resolution
        elif live and pending:
            stage = "in_progress"
        elif submitted:
            stage = "submitted" if live else "unconfirmed"
        elif live:
            stage = decision
        else:
            stage = "unrecorded"
        row["stage"] = stage
        counts["submitted"] += bool(submitted)
        counts["in_progress"] += stage == "in_progress"
        counts["accepted"] += stage == "completed"
    return {"steps": rows, "counts": counts, "unlinked": unlinked, "explanation": plan.get("explanation", "")}
