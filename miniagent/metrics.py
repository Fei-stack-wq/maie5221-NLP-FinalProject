"""Evaluation metrics derived from task records (``tasks/<id>/task.json``).

Each task record is written by :class:`miniagent.team.TeamTask` from the
agents' actual results — status, steps, tool calls and provider-reported
token usage — so every number here traces back to a run.
"""

from __future__ import annotations

from .store import GroupStore


def summarize(tasks: list[dict]) -> dict:
    finished = [t for t in tasks if t.get("status") not in {None, "running"}]
    completed = [t for t in finished if t["status"] == "completed"]
    durations = sorted(t.get("metrics", {}).get("duration_s", 0) for t in finished if t.get("metrics"))
    tokens = sum(t.get("metrics", {}).get("prompt_tokens", 0) + t.get("metrics", {}).get("completion_tokens", 0)
                 for t in finished)
    p95 = durations[min(len(durations) - 1, int(round(0.95 * (len(durations) - 1))))] if durations else 0
    return {
        "tasks": len(finished),
        "completed": len(completed),
        "completion_rate": round(len(completed) / len(finished), 3) if finished else 0.0,
        "mean_duration_s": round(sum(durations) / len(durations), 1) if durations else 0.0,
        "p95_duration_s": p95,
        "total_tokens": tokens,
        "delegations": sum(t.get("delegations", 0) for t in finished),
        "questions": sum(t.get("metrics", {}).get("questions", 0) for t in finished),
    }


def group_report(store: GroupStore) -> str:
    tasks = store.tasks()
    summary = summarize(tasks)
    lines = [f"## {store.group.name} ({store.group.id})", "",
             f"Tasks {summary['tasks']} · completed {summary['completed']} "
             f"({summary['completion_rate']:.0%}) · mean {summary['mean_duration_s']}s · "
             f"p95 {summary['p95_duration_s']}s · tokens {summary['total_tokens']:,} · "
             f"delegations {summary['delegations']} · questions {summary['questions']}", "",
             "| task | status | duration s | steps | tool calls | delegations | questions | tokens | request |",
             "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |"]
    for task in tasks:
        m = task.get("metrics", {})
        text = " ".join(task.get("text", "").split())[:60].replace("|", "/")
        lines.append(f"| {task['id']} | {task.get('status')} | {m.get('duration_s', '')} | {m.get('steps', '')} | "
                     f"{m.get('tool_calls', '')} | {task.get('delegations', 0)} | {m.get('questions', '')} | "
                     f"{m.get('prompt_tokens', 0) + m.get('completion_tokens', 0):,} | {text} |")
        for item in task.get("assignments", []):
            lines.append(f"|  ↳ {item['agent']} | {item['status']} | {item['duration_s']} | {item['steps']} | "
                         f"{item['tool_calls']} |  |  | {item['prompt_tokens'] + item['completion_tokens']:,} |  |")
    return "\n".join(lines)
