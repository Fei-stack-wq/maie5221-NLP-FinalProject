"""Tracer / 指标汇总。

对应课程要求「系统框架必须在评估过程中自动跟踪并报告」：
  - 任务完成率：completed / total
  - 执行时间：每个任务的 duration_ms，以及均值/P95

为什么一开始就要有：报告里的每个数字都必须能追溯到某次 run 的某条 trace。
事后补日志是补不出来的 —— 因为你会忘记当时到底发生了什么。
"""

from __future__ import annotations

import json
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


class Tracer:
    """把每一个事件追加写入 ``run_dir/trace.jsonl``（一行一个 JSON）。

    用 JSONL 而不是数据库：可读、可 grep、可直接贴进报告、永不写坏。
    数据量大了再换 SQLite —— 这个项目大概率用不上。
    """

    def __init__(self, run_dir: Path | str) -> None:
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.trace_path = self.run_dir / "trace.jsonl"
        self._handle = self.trace_path.open("a", encoding="utf-8")
        # 并发调度（S2）时多个 agent 线程同时写同一文件，必须串行化，
        # 否则会出现半行 JSON —— 那是汇总脚本最难查的一类脏数据。
        self._lock = threading.Lock()

    def event(self, kind: str, **payload: Any) -> None:
        record = {
            "ts": round(time.time(), 4),
            "iso": datetime.now().isoformat(timespec="milliseconds"),
            "kind": kind,
            **payload,
        }
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._lock:
            self._handle.write(line)
            self._handle.flush()  # 崩溃时也要保住已有证据

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()

    def __enter__(self) -> "Tracer":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def load_events(run_dir: Path | str) -> list[dict[str, Any]]:
    path = Path(run_dir) / "trace.jsonl"
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # 半行（进程被杀）不该让报告脚本崩掉
    return events


def _percentile(sorted_values: list[float], ratio: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, max(0, int(round(ratio * (len(sorted_values) - 1)))))
    return sorted_values[index]


def summarize(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """从事件流里算出报告需要的那几个数。"""
    events = list(events)
    # 总体目标走 level="goal"（编排器写），其余 task_end 是子任务/单任务。
    # 两者混在一起算，完成率就没有意义了。
    ends = [
        event
        for event in events
        if event.get("kind") == "task_end" and event.get("level") != "goal"
    ]
    goals = [event for event in events if event.get("kind") == "orchestration_end"]
    steps = [event for event in events if event.get("kind") == "step"]

    total = len(ends)
    completed = [event for event in ends if event.get("status") == "completed"]
    durations = sorted(float(event.get("duration_ms") or 0.0) for event in ends)
    failures = Counter(
        str(event.get("error") or event.get("status")) for event in ends if event.get("status") != "completed"
    )
    tokens = sum(
        int(event.get("prompt_tokens") or 0) + int(event.get("completion_tokens") or 0)
        for event in steps
    )

    # 子任务层（编排器运行时才有）：课程要求同时报告"子任务完成率"与"总体完成率"
    sub_ends = [event for event in events if event.get("kind") == "subtask_end"]
    sub_done = [event for event in sub_ends if event.get("status") == "done"]
    sub_durations = sorted(float(event.get("duration_ms") or 0.0) for event in sub_ends)
    sub_failures = Counter(
        str(event.get("error") or event.get("status"))
        for event in sub_ends
        if event.get("status") != "done"
    )

    return {
        "total_tasks": total,
        "completed": len(completed),
        "completion_rate": (len(completed) / total) if total else 0.0,
        "total_duration_ms": round(sum(durations), 2),
        "mean_duration_ms": round(sum(durations) / total, 2) if total else 0.0,
        "p95_duration_ms": round(_percentile(durations, 0.95), 2),
        "mean_steps": round(len(steps) / total, 2) if total else 0.0,
        "total_tokens": tokens,
        "mean_tokens_per_task": round(tokens / total) if total else 0,
        "failure_modes": dict(failures),
        "subtask_total": len(sub_ends),
        "subtask_completed": len(sub_done),
        "subtask_completion_rate": (len(sub_done) / len(sub_ends)) if sub_ends else 0.0,
        "subtask_mean_duration_ms": (
            round(sum(sub_durations) / len(sub_durations), 2) if sub_durations else 0.0
        ),
        "subtask_failure_modes": dict(sub_failures),
        "goal_status": str((goals[-1] if goals else {}).get("status") or ""),
        "goal_duration_ms": round(
            float((goals[-1] if goals else {}).get("duration_ms") or 0.0), 2
        ),
    }


def render_report(run_dir: Path | str, write: bool = True) -> str:
    """生成一份可以直接贴进交报告的 Markdown 表。"""
    run_dir = Path(run_dir)
    events = load_events(run_dir)
    summary = summarize(events)
    ends = [
        event
        for event in events
        if event.get("kind") == "task_end" and event.get("level") != "goal"
    ]

    lines = [
        f"# 运行报告：{run_dir.name}",
        "",
        "## 总览",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 任务总数 | {summary['total_tasks']} |",
        f"| 完成数 | {summary['completed']} |",
        f"| **任务完成率** | **{summary['completion_rate']:.1%}** |",
        f"| 总耗时 | {summary['total_duration_ms'] / 1000:.2f} s |",
        f"| 平均耗时 | {summary['mean_duration_ms'] / 1000:.2f} s |",
        f"| P95 耗时 | {summary['p95_duration_ms'] / 1000:.2f} s |",
        f"| 平均步数 | {summary['mean_steps']} |",
        f"| 总 token | {summary['total_tokens']} |",
        f"| 平均 token/任务 | {summary['mean_tokens_per_task']} |",
        "",
        "## 失败模式",
        "",
    ]
    if summary["failure_modes"]:
        lines += ["| 原因 | 次数 |", "|---|---|"]
        lines += [f"| {reason} | {count} |" for reason, count in summary["failure_modes"].items()]
    else:
        lines.append("无失败任务。")

    sub_ends = [event for event in events if event.get("kind") == "subtask_end"]
    if sub_ends:
        lines += [
            "",
            "## 总体目标",
            "",
            f"- 状态：**{summary['goal_status'] or '-'}**",
            f"- 耗时：{summary['goal_duration_ms'] / 1000:.2f} s",
            "",
            "## 子任务",
            "",
            f"- 子任务完成率：**{summary['subtask_completion_rate']:.1%}**"
            f"（{summary['subtask_completed']}/{summary['subtask_total']}）",
            f"- 平均子任务耗时：{summary['subtask_mean_duration_ms'] / 1000:.2f} s",
            "",
            "| 子任务 | agent | 状态 | 步数 | 耗时(s) | token |",
            "|---|---|---|---|---|---|",
        ]
        for event in sub_ends:
            lines.append(
                "| {sid} | {agent} | {status} | {steps} | {duration:.2f} | {tokens} |".format(
                    sid=event.get("subtask", "-"),
                    agent=event.get("assignee", "-"),
                    status=event.get("status", "-"),
                    steps=event.get("steps", 0),
                    duration=float(event.get("duration_ms") or 0) / 1000,
                    tokens=event.get("tokens", 0),
                )
            )
        if summary["subtask_failure_modes"]:
            lines += ["", "失败子任务原因："]
            lines += [f"- {reason}：{count} 次" for reason, count in summary["subtask_failure_modes"].items()]

    lines += ["", "## 逐任务明细", "", "| 任务 | agent | 状态 | 步数 | 工具调用 | 耗时(s) | token |", "|---|---|---|---|---|---|---|"]
    for event in ends:
        lines.append(
            "| {task} | {agent} | {status} | {steps} | {tools} | {duration:.2f} | {tokens} |".format(
                task=event.get("task_id", "-"),
                agent=event.get("agent", "-"),
                status=event.get("status", "-"),
                steps=event.get("steps", 0),
                tools=event.get("tool_calls", 0),
                duration=float(event.get("duration_ms") or 0) / 1000,
                tokens=int(event.get("prompt_tokens") or 0) + int(event.get("completion_tokens") or 0),
            )
        )

    report = "\n".join(lines) + "\n"
    if write:
        (run_dir / "report.md").write_text(report, encoding="utf-8")
    return report
