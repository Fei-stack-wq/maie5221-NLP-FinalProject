"""Project a lead's plan and the work the team actually recorded onto the same steps."""
import re
from collections import defaultdict
from .review import review_verdict


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
    lead_seq = row["evidence"].get("lead_work", {}).get("seq", 0)
    decision = declaration.get("status", row["status"])
    reported_seq = row["evidence"].get("submitted", {}).get("seq", 0)
    if (not outstanding and decision in ("completed", "rework")
            and declaration.get("seq", 0) >= max(assigned.get("seq", 0), lead_seq, reported_seq)):
        return {"stage": decision, "source": "lead", "agent": declaration.get("agent", lead)}

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


def evidence_steps(event):
    """The primary ID remains readable by older Studio clients."""
    return list(dict.fromkeys(([event["step"]] if event.get("step") else []) + event.get("step_ids", [])))


def _linked_work(record, events):
    """Replay references at the original event position. Linking an old review never makes it new."""
    added = defaultdict(list)
    sources = {e["seq"]: e for e in events}
    for e in events:
        if e["kind"] == "plan_evidence":
            for link in e["links"]:
                added[link["source_seq"]].extend(link["step_ids"])
                source = sources.get(link["source_seq"], {})
                if source.get("kind") == "report" and source.get("assignment_seq"):
                    added[source["assignment_seq"]].extend(link["step_ids"])
    assignments = {}
    open_assignments = {}
    # The final snapshot can already have different owners or outputs. Apply the
    # plan in force when the original work was performed.
    steps = record["plan"]["steps"] if not any(e["kind"] == "plan" for e in events) else []
    publications = {}
    for e in events:
        kind = e["kind"]
        if kind == "plan":
            steps = e["steps"]
            yield None, e
            continue
        if kind not in ("delegation", "report", "publish", "final", "plan_work", "plan_decision"):
            continue
        ids = evidence_steps(e) + added[e["seq"]]
        if kind == "delegation":
            assignee = e["assignee"]
            if e.get("reconcile"):
                prior = e.get("continues_assignment", open_assignments.get(assignee))
                if prior in assignments:
                    ids.extend(assignments[prior])
                    e = {**e, "continues_assignment": prior}
            assignments[e["seq"]] = ids
            open_assignments[assignee] = e["seq"]
        elif kind == "report":
            original = e.get("assignment_seq")
            ids.extend(assignments.get(original, []))
            if open_assignments.get(e["agent"]) == original:
                open_assignments.pop(e["agent"], None)
        elif e.get("agent") == record.get("primary"):
            if kind == "publish":
                changes = {c["path"]: c for c in e["changes"]}
                for path, change in changes.items():
                    if change["change"] == "deleted":
                        publications.pop(path, None)
                    else:
                        publications[path] = change
                for step in steps:
                    outputs = step.get("outputs", [])
                    if (step.get("owner") == record["primary"] and changes.keys() & set(outputs)
                            and step["id"] not in ids):
                        files = [publications[path] for path in outputs if path in publications]
                        yield step["id"], {**e, "changes": files, "work_complete": len(files) == len(outputs)}
            elif kind == "final" and e.get("status") == "completed":
                ids += [s["id"] for s in steps if s.get("on_delivery") and s.get("owner") == record["primary"]]
        if not ids and kind in ("delegation", "report"):
            yield None, e
        for key in dict.fromkeys(ids):
            yield key, e


def plan_view(record, events):
    """Project work performed and acceptance independently from the same task-scoped history."""
    plan = record.get("plan") or {}
    if not plan.get("linked"):
        return None
    rows = [{**row, "evidence": {}, "history": []} for row in plan["steps"]]
    by_id = {row["id"]: row for row in rows}
    unlinked = []
    unlinked_by_seq = {}
    unlinked_open = defaultdict(list)
    outstanding = defaultdict(set)
    deliveries = defaultdict(dict)
    declarations = {}
    plan_statuses = {}
    review_of_completed = defaultdict(set)
    task_events = [e for e in events if e.get("task_id") == record["id"]]
    for key, event in _linked_work(record, task_events):
        kind = event["kind"]
        if kind == "plan":
            for step in event["steps"]:
                key = step.get("id")
                if key in by_id and plan_statuses.get(key) != step["status"]:
                    if key in plan_statuses and step["status"] in ("pending", "in_progress", "rework"):
                        deliveries[key].clear()
                        review_of_completed[key].clear()
                    plan_statuses[key] = step["status"]
                    declarations[key] = {"status": step["status"], "seq": event["seq"], "agent": event["agent"]}
            continue
        row = by_id.get(key)
        if row is None:
            if kind == "delegation" and event["seq"] not in unlinked_by_seq:
                item = {k: event[k] for k in ("seq", "assignee", "task", "step", "requested_step", "handoff_kind") if k in event}
                unlinked.append(item)
                unlinked_by_seq[event["seq"]] = item
                unlinked_open[event["assignee"]].append(item)
            elif kind == "report":
                assignment = event.get("assignment_seq")
                if assignment is not None:
                    item = unlinked_by_seq.get(assignment)
                elif unlinked_open.get(event.get("agent")):
                    item = unlinked_open[event["agent"]].pop(0)
                else:
                    item = None
                if item is not None and "status" not in item:
                    item["status"] = event.get("status", "completed")
                    queue = unlinked_open.get(item["assignee"])
                    if queue and item in queue:
                        queue.remove(item)
            continue
        evidence = row["evidence"]
        entry = {"seq": event["seq"], "time": event["time"], "kind": kind}
        if kind == "delegation":
            entry.update(agent=event["assignee"], task=event["task"])
            if event.get("handoff_kind"):
                entry["handoff_kind"] = event["handoff_kind"]
            if event.get("inferred_step"):
                entry["inferred_step"] = True
            if event.get("review"):
                entry["review"] = True
            evidence["assigned"] = entry
            pending = outstanding[key]
            if event.get("review") and deliveries[key] and all(deliveries[key].values()) and not (pending - review_of_completed[key]):
                review_of_completed[key].add(event["seq"])
            elif not pending:
                deliveries[key].clear()
            # Parallel reconciliation continues an existing assignment.
            pending.discard(event.get("continues_assignment"))
            pending.add(event["seq"])
        elif kind == "report":
            assignment = event.get("assignment_seq")
            outstanding[key].discard(assignment)
            entry.update(agent=event["agent"], status=event["status"], files=event.get("changes", []),
                         assignment_seq=assignment)
            if event.get("handoff_kind"):
                entry["handoff_kind"] = event["handoff_kind"]
            if event.get("checks"):
                entry["checks"] = {k: event["checks"][k] for k in ("passed", "total")}
            checks = entry.get("checks", {})
            deliveries[key][assignment] = (event["status"] == "completed" and not event.get("missing_deliverables")
                                            and not event.get("unpublished_files")
                                            and checks.get("total") == checks.get("passed"))
            if event.get("missing_deliverables"):
                entry["missing_deliverables"] = event["missing_deliverables"]
            if event.get("unpublished_files"):
                entry["unpublished_files"] = event["unpublished_files"]
            evidence["submitted"] = entry
            if event.get("review"):
                verdict = event.get("verdict")
                if verdict in ("approved", "changes requested") and event.get("text"):
                    verdict = review_verdict(event["text"])
                entry["verdict"] = verdict or "unclear"
                evidence["reviewed"] = entry
                if verdict == "changes requested":
                    deliveries[key][assignment] = False
        elif kind == "plan_decision":
            declarations[key] = event
            entry.update(agent=event["agent"], summary=event["text"], status=event["status"])
            if event["status"] == "rework":
                deliveries[key].clear()
        else:
            entry.update(agent=event["agent"], files=event.get("changes", []))
            if kind in ("plan_work", "final"):
                entry["summary"] = event.get("text", "")
            complete = (event.get("work_complete", any(f["change"] != "deleted" for f in entry["files"]))
                        if kind == "publish" else event.get("status") == "completed" and bool(event.get("text")))
            deliveries[key]["lead"] = complete
            evidence["lead_work"] = entry
        if event.get("loop"):
            entry["loop"] = event["loop"]
        row["history"].append(entry)

    live = record.get("status") in ("running", "queued", "paused")
    for item in unlinked:
        status = item.get("status")
        if status == "completed":
            item["stage"] = "completed"
        elif status:
            item["stage"] = "incomplete"
        elif live:
            item["stage"] = "in_progress"
        else:
            item["stage"] = "unrecorded"
        item["active"] = item["stage"] == "in_progress"
    counts = dict(total=len(rows), completed=0, submitted=0, in_progress=0, confirmed=0)
    for row in rows:
        key, evidence = row["id"], row["evidence"]
        pending = outstanding[key]
        declared = declarations.get(key, {})
        resolution = _step_decision(row, declared, pending, record.get("primary"))
        work = deliveries[key]
        complete = bool(work) and all(work.values()) and not (pending - review_of_completed[key])
        if resolution and resolution["source"] == "review" and resolution["stage"] == "completed" and not complete:
            resolution = None
        if resolution:
            row["decision"] = resolution
            complete = resolution["stage"] == "completed"
        if complete:
            stage = "completed"
        elif resolution:
            stage = resolution["stage"]
        elif live and (pending or row["status"] == "in_progress"):
            stage = "in_progress"
        elif evidence.get("submitted") or evidence.get("lead_work"):
            stage = "incomplete"
        else:
            stage = "pending" if live else "unrecorded"
        row["stage"] = stage
        row["completed"] = complete
        row["confirmed"] = bool(resolution and resolution["stage"] == "completed")
        counts["submitted"] += "submitted" in evidence
        counts["in_progress"] += stage == "in_progress"
        counts["confirmed"] += row["confirmed"]
        counts["completed"] += complete
    counts["accepted"] = counts["confirmed"]  # Older Studio clients read this name.
    return {"steps": rows, "counts": counts, "unlinked": unlinked, "explanation": plan.get("explanation", "")}
