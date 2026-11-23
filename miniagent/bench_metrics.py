"""Collaboration measurements from persisted, task-scoped evidence."""
import difflib
import json


def collaboration_metrics(store, record):
    events = [e for e in store.chat(after=record.get("trigger_seq", 0)) if e.get("task_id") == record["id"]]
    assignments = [e for e in events if e["kind"] == "delegation"]
    linked = sum(bool(e.get("step")) for e in assignments)
    previous = {}
    repeats = 0
    for event in assignments:
        history = previous.setdefault(event["assignee"], [])
        brief = event["task"][:3000]
        repeats += any(difflib.SequenceMatcher(None, brief, old).ratio() >= 0.9 for old in history)
        history.append(brief)
    # Review ownership comes from actual assignments. A plain browser run without
    # a named verifier provides no basis for assigning blame for duplicate checks.
    owners = {e["assignee"] for e in assignments if e.get("review") or (e.get("loop") or {}).get("role") == "review"}
    seconds = {"owner": 0.0, "non_owner": 0.0, "unattributed": 0.0}
    plan_calls = 0
    trace = store.task_dir(record["id"]) / "trace.jsonl"
    if trace.is_file():
        with trace.open(encoding="utf-8") as source:
            for line in source:
                event = json.loads(line)
                if event["type"] == "tool_call" and event.get("tool") == "update_plan":
                    plan_calls += 1
                if event["type"] == "tool_result" and event.get("tool") == "run_browser":
                    owner = "unattributed" if not owners else "owner" if event["agent"] in owners else "non_owner"
                    seconds[owner] += event.get("seconds", 0)
    comm = (record.get("metrics", {}).get("time") or {}).get("communication", {})
    # A reconciliation continues the same assignment and carries its cumulative counters.
    talk = {a["assignment_seq"]: a["talk"] for a in record.get("assignments", [])
            if "assignment_seq" in a and "talk" in a}
    limits = record.get("metrics", {}).get("communication_limits", {})
    at_limit = sum(any(limit and counts.get(name, 0) >= limit for name, limit in limits.items())
                   for counts in talk.values())
    return {"plan_link_rate": round(linked / len(assignments), 4) if assignments else None,
            "linked_hand_offs": linked, "update_plan_calls": plan_calls, "re_delegations": repeats,
            "browser_minutes": {k: round(v / 60, 3) for k, v in seconds.items()},
            "non_owner_browser_minutes": round(seconds["non_owner"] / 60, 3) if owners else None,
            "talk_assignments": len(talk), "talk_assignments_at_limit": at_limit,
            "talk_below_limit_rate": round(1 - at_limit / len(talk), 4) if talk and limits else None, **comm}
