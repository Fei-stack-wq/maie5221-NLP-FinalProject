"""Trace-derived logical task metrics with explicit acceptance evidence.

This validates recorded reports, not the external facts asserted by evaluators.
Missing terminal events remain in the denominator. Historical ``completed``
labels without a bound report never become verified successes.
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .contracts.acceptance import AcceptanceReport, AcceptanceSpec, validate_report
from .acceptance_evidence import validate_evidence


class Tracer:
    """把每一个事件追加写入 ``run_dir/trace.jsonl``（一行一个 JSON）。

    JSONL 是审计记录；崩溃可能留下半行，不替代事务存储。
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
            "schema_version": 2,
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
            record = json.loads(line)
            if isinstance(record, dict):
                events.append(record)
        except json.JSONDecodeError:
            continue  # 半行（进程被杀）不该让报告脚本崩掉
    return events


def _percentile(sorted_values: list[float], ratio: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, max(0, int(round(ratio * (len(sorted_values) - 1)))))
    return sorted_values[index]


_STARTS = {"task_start", "subtask_start", "orchestration_start"}
_ENDS = {"task_end", "subtask_end", "orchestration_end"}
_REGISTRATIONS = {"case_registered", "task_registered"}
_USAGE_EVENTS = {"step", "plan_usage", "provider_error"}
_TASK_EVENTS = _STARTS | _ENDS | _REGISTRATIONS | {"acceptance"} | _USAGE_EVENTS
_SUCCESS = {"completed", "done"}


def _id(value: Any) -> str:
    return value if isinstance(value, str) and value.strip() else ""


def _number(value: Any) -> float | None:
    if type(value) in (int, float) and math.isfinite(value) and value >= 0:
        return float(value)
    return None


def _fingerprint(event: dict[str, Any]) -> str:
    return json.dumps(event, sort_keys=True, ensure_ascii=False, default=str)


def _case_id(case: str | dict[str, Any]) -> str:
    if isinstance(case, str):
        identity = _id(case)
    elif isinstance(case, dict):
        identity = _id(case.get("root_id") or case.get("task_id") or case.get("id"))
    else:
        identity = ""
    if not identity:
        raise ValueError("expected_cases entries require a non-blank root/task ID")
    return identity


@dataclass
class _Attempt:
    identity: str
    order: int
    started: bool = False
    terminals: list[tuple[int, dict[str, Any]]] = field(default_factory=list)
    acceptance: list[tuple[int, dict[str, Any]]] = field(default_factory=list)


@dataclass
class _Task:
    root_id: str
    task_id: str
    level: str
    parent_task_id: str | None = None
    agent: str = ""
    attempts: dict[str, _Attempt] = field(default_factory=dict)

    def observe(self, event: dict[str, Any], position: int) -> None:
        self.agent = str(event.get("assignee") or event.get("agent") or self.agent)
        parent = _id(event.get("parent_task_id") or event.get("parent_id"))
        if parent:
            self.parent_task_id = parent
        kind = event.get("kind")
        identity = _id(event.get("attempt_id"))
        if kind in _REGISTRATIONS:
            if identity:
                self.attempts.setdefault(identity, _Attempt(identity, position))
            return
        attempt = self.attempts.setdefault(identity, _Attempt(identity, position))
        if kind in _STARTS or kind in _USAGE_EVENTS or kind == "acceptance":
            attempt.started = True
        if kind in _ENDS:
            attempt.terminals.append((position, event))
        elif kind == "acceptance":
            attempt.acceptance.append((position, event))


def _terminal(attempt: _Attempt) -> tuple[int, dict[str, Any]] | None:
    if not attempt.terminals:
        return None
    # A controller projection is more complete than the nested agent's end
    # event, but both describe the same logical task and attempt.
    return max(attempt.terminals, key=lambda pair: (
        pair[1].get("kind") in {"orchestration_end", "subtask_end"}, pair[0],
    ))


def _verification(task: _Task, attempt: _Attempt, end: dict[str, Any], position: int) -> tuple[bool, str, str | None]:
    if type(end.get("schema_version")) is not int or end.get("schema_version") != 2:
        return False, "legacy_completion_without_verified_report", None
    if not attempt.identity or end.get("task_id") != task.task_id or end.get("root_id") != task.root_id:
        return False, "acceptance_identity_missing_or_mismatched", None
    source = end
    if "acceptance_report" not in end:
        prior = [event for index, event in attempt.acceptance if index <= position]
        if prior:
            source = prior[-1]
    raw_report = source.get("acceptance_report", source.get("report"))
    raw_spec = source.get("acceptance_spec", source.get("spec"))
    if raw_report is None:
        return False, "missing_acceptance_report", None
    try:
        report = AcceptanceReport.model_validate(raw_report)
        spec = AcceptanceSpec.model_validate(raw_spec) if raw_spec is not None else None
        validate_report(spec, report)
        if report.task_id != task.task_id or report.attempt_id != attempt.identity:
            return False, "acceptance_identity_mismatch", report.report_id
        if report.verdict != "passed":
            return False, f"acceptance_{report.verdict}", report.report_id
        if spec is None:
            return False, "missing_acceptance_spec", report.report_id
        target_ids = set(spec.target.resource_ids)
        if not target_ids.issubset({value.resource_id for value in report.evaluated_resource_versions}):
            return False, "acceptance_target_not_covered", report.report_id
        receipts = []
        direct = end.get("acceptance_evidence")
        if direct is not None:
            if not isinstance(direct, (list, tuple)):
                return False, "invalid_acceptance_evidence", report.report_id
            receipts.extend(direct)
        for index, event in attempt.acceptance:
            if index > position:
                continue
            event_report = event.get("acceptance_report", event.get("report"))
            if not isinstance(event_report, dict) or event_report.get("report_id") != report.report_id:
                continue
            if AcceptanceReport.model_validate(event_report) != report:
                return False, "conflicting_acceptance_report", report.report_id
            event_spec = event.get("acceptance_spec", event.get("spec"))
            if AcceptanceSpec.model_validate(event_spec) != spec:
                return False, "conflicting_acceptance_spec", report.report_id
            event_receipts = event.get("receipts", event.get("acceptance_evidence", []))
            if not isinstance(event_receipts, (list, tuple)):
                return False, "invalid_acceptance_evidence", report.report_id
            receipts.extend(event_receipts)
        try:
            validate_evidence(spec, report, receipts)
        except (ValueError, TypeError):
            return False, "invalid_acceptance_evidence", report.report_id
        return True, "", report.report_id
    except (ValueError, TypeError):
        return False, "invalid_acceptance_report", None


def _duration(attempts: list[_Attempt], field_name: str) -> tuple[float | None, float]:
    known: list[float] = []
    complete = bool(attempts)
    for attempt in attempts:
        terminal = _terminal(attempt)
        value = _number(terminal[1].get(field_name)) if terminal else None
        if value is None:
            complete = False
        else:
            known.append(value)
    subtotal = round(sum(known), 2)
    return (subtotal if complete else None), subtotal


def _outcome(task: _Task) -> dict[str, Any]:
    attempts = sorted(task.attempts.values(), key=lambda attempt: attempt.order)
    current = attempts[-1] if attempts else None
    terminal = _terminal(current) if current else None
    status = "incomplete" if current and current.started else "not_started"
    raw_status = status
    reason = "missing_terminal_event" if status == "incomplete" else "case_not_started"
    verified, report_id = False, None
    if terminal:
        position, end = terminal
        raw_status = str(end.get("status") or "error")
        status = raw_status
        reason = str(end.get("error") or end.get("reason") or "")
        candidate, validation_reason, report_id = _verification(task, current, end, position)
        if raw_status in _SUCCESS:
            verified = candidate
            # A fatal/non-success terminal cannot be erased by another success
            # for the same attempt. A real retry must have a new attempt ID.
            conflicting = any(
                other.get("schema_version") == 2 and other.get("status") not in _SUCCESS
                for _, other in current.terminals
            )
            if conflicting:
                verified, status, reason = False, "error", "conflicting_terminal_events"
            elif verified:
                status = "completed" if task.level == "goal" else "done"
            else:
                status, reason = "unverified", reason or validation_reason
    duration, known_duration = _duration(attempts, "duration_ms")
    active, _ = _duration(attempts, "active_duration_ms")
    waiting, _ = _duration(attempts, "wait_duration_ms")
    tools, _ = _duration(attempts, "tool_calls")
    return {
        "root_id": task.root_id, "task_id": task.task_id, "level": task.level,
        "parent_task_id": task.parent_task_id, "agent": task.agent,
        "attempt_id": current.identity if current else None,
        "attempts": len(attempts), "status": status, "source_status": raw_status,
        "verified": verified, "report_id": report_id, "error": reason,
        "duration_ms": duration, "known_duration_ms": known_duration,
        "active_duration_ms": active, "wait_duration_ms": waiting,
        "tool_calls": int(tools) if tools is not None else None,
        "steps": 0, "tokens": 0,
    }


def _usage_key(event: dict[str, Any], root_id: str, task_id: str) -> tuple:
    identity = (root_id, task_id, _id(event.get("attempt_id")), _id(event.get("run_id")))
    if _id(event.get("call_id")):
        return identity + ("call", event["call_id"])
    sequence = event.get("step") if event.get("kind") == "step" else event.get("plan_attempt", event.get("attempt"))
    if sequence is not None:
        return identity + (event.get("kind"), str(sequence))
    return identity + ("event", _fingerprint(event))


@dataclass(frozen=True)
class _UsageMeasurement:
    tokens: int
    estimated: bool
    unknown: bool
    attempts: int
    unknown_attempts: int
    missing_attempt_count: bool = False


def _usage(event: dict[str, Any]) -> _UsageMeasurement:
    payload = event.get("usage") if isinstance(event.get("usage"), dict) else event
    values = [payload.get("prompt_tokens"), payload.get("completion_tokens")]
    tokens = sum(value for value in values if type(value) is int and value >= 0)
    complete = all(type(value) is int and value >= 0 for value in values)
    failed = event.get("kind") == "provider_error"
    source = event.get("usage_source", payload.get("source", "reported"))
    if failed or type(source) is not str or source not in {"reported", "estimated", "unknown"}:
        source = "unknown"
    # A logical provider call is not proof of an HTTP request: scripted/local
    # providers use the same response schema. Legacy traces without transport
    # provenance retain their token information but cannot establish HTTP cost.
    transport = event.get("transport")
    unknown_usage = source == "unknown" or not complete
    if transport == "offline":
        return _UsageMeasurement(tokens, source == "estimated", unknown_usage, 0, 0)
    if transport != "http":
        return _UsageMeasurement(tokens, source == "estimated", unknown_usage, 0, 0, True)
    # An explicit HTTP transport still needs a recorded attempt count. Zero is
    # valid only for a failure before any HTTP attempt was admitted.
    attempts = event.get("request_attempts")
    if type(attempts) is not int or attempts < (0 if failed else 1):
        return _UsageMeasurement(tokens, source == "estimated", True, 0, 0, True)
    unknown_attempts = attempts if failed else (
        attempts - 1 + int(unknown_usage)
    )
    # Preserve partial/estimated numbers, but do not label unknown usage as a
    # real zero. Failed HTTP retries have no usage receipt even if the final
    # successful response reports its own tokens accurately.
    return _UsageMeasurement(tokens, source == "estimated", unknown_attempts > 0,
                             attempts, unknown_attempts)


def summarize(
    events: Iterable[dict[str, Any]],
    expected_cases: Iterable[str | dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Summarize roots and logical children, not terminal-event occurrences.

    ``expected_cases`` is an optional fixed manifest of root IDs or dictionaries
    containing root_id/task_id/id. When supplied, extra observed roots are listed
    separately and cannot change Goal TCR. Otherwise registration/start/end
    events establish the denominator. Retry attempts never add logical tasks.
    Duration totals are sums of recorded per-attempt wall durations, not the
    wall duration of a concurrent batch. Unknown active/wait time stays null.
    """
    events = [event for event in events if isinstance(event, dict)]
    expected = list(dict.fromkeys(_case_id(case) for case in expected_cases)) if expected_cases is not None else None
    roots: dict[str, _Task] = {}
    children: dict[tuple[str, str], _Task] = {}

    def root(identity: str) -> _Task:
        return roots.setdefault(identity, _Task(identity, identity, "goal"))

    for identity in expected or ():
        root(identity)
    legacy_roots = {
        _id(event.get("root_id") or event.get("task_id"))
        for event in events if event.get("kind") in {"orchestration_start", "orchestration_end"}
    } - {""}
    legacy_subtasks = {
        _id(event.get("task_id") or event.get("subtask"))
        for event in events if event.get("kind") in {"subtask_start", "subtask_end"}
    } - {""}
    context_root = ""
    usage_events: dict[tuple, tuple[dict[str, Any], str, str]] = {}
    for position, event in enumerate(events):
        kind = event.get("kind")
        if kind not in _TASK_EVENTS:
            continue
        task_id = _id(event.get("task_id") or event.get("subtask"))
        root_id = _id(event.get("root_id"))
        if kind in {"orchestration_start", "orchestration_end", "case_registered"}:
            root_id = root_id or task_id
            task_id = root_id
            level = "goal"
        elif event.get("level") == "goal":
            root_id = root_id or task_id
            level = "goal"
        elif event.get("level") == "subtask" or kind in {"subtask_start", "subtask_end"} or (root_id and root_id != task_id):
            level = "subtask"
        elif not root_id and task_id in legacy_subtasks:
            level = "subtask"
        else:
            level = "goal"
            root_id = root_id or task_id
        if not task_id:
            continue
        if kind == "orchestration_start":
            context_root = root_id
        if level == "subtask":
            root_id = root_id or context_root or (next(iter(legacy_roots)) if len(legacy_roots) == 1 else "")
            if root_id:
                root(root_id)
            task = children.setdefault((root_id, task_id), _Task(root_id, task_id, level))
        else:
            task = root(root_id)
        task.observe(event, position)
        if kind in _USAGE_EVENTS:
            key = _usage_key(event, root_id, task_id)
            # Identical replay costs nothing extra. A later copy may contain a
            # finalized provider usage receipt for the same model call.
            usage_events[key] = (event, root_id, task_id)
        if kind == "orchestration_end" and context_root == root_id:
            context_root = ""

    all_goals = {identity: _outcome(task) for identity, task in roots.items()}
    all_subtasks = {identity: _outcome(task) for identity, task in children.items()}
    total_tokens = unknown_usage = estimated_usage = step_count = 0
    http_attempts = unknown_attempts = missing_attempt_counts = 0
    for event, root_id, task_id in usage_events.values():
        usage = _usage(event)
        total_tokens += usage.tokens
        unknown_usage += usage.unknown
        estimated_usage += usage.estimated
        http_attempts += usage.attempts
        unknown_attempts += usage.unknown_attempts
        missing_attempt_counts += usage.missing_attempt_count
        is_step = event.get("kind") == "step"
        step_count += is_step
        if root_id in all_goals:
            all_goals[root_id]["tokens"] += usage.tokens
            all_goals[root_id]["steps"] += is_step
        child = all_subtasks.get((root_id, task_id))
        if child is not None:
            child["tokens"] += usage.tokens
            child["steps"] += is_step

    selected = set(expected) if expected is not None else set(all_goals)
    goals = [row for identity, row in all_goals.items() if identity in selected]
    unexpected = [row for identity, row in all_goals.items() if identity not in selected]
    subtasks = [row for row in all_subtasks.values() if expected is None or row["root_id"] in selected]
    durations = sorted(row["duration_ms"] for row in goals if row["duration_ms"] is not None)
    sub_durations = sorted(row["duration_ms"] for row in subtasks if row["duration_ms"] is not None)
    total, completed = len(goals), sum(row["verified"] for row in goals)
    sub_total, sub_completed = len(subtasks), sum(row["verified"] for row in subtasks)
    goal_counts = dict(Counter(row["status"] for row in goals))
    sub_counts = dict(Counter(row["status"] for row in subtasks))
    failures = dict(Counter(row["error"] or row["status"] for row in goals if not row["verified"]))
    sub_failures = dict(Counter(row["error"] or row["status"] for row in subtasks if not row["verified"]))
    root_duration = round(sum(row["known_duration_ms"] for row in goals), 2)
    sub_duration = round(sum(row["known_duration_ms"] for row in subtasks), 2)

    def clock_total(name: str) -> float | None:
        values = [row[name] for row in goals]
        return round(sum(values), 2) if values and all(value is not None for value in values) else None

    return {
        "total_tasks": total, "completed": completed,
        "completion_rate": completed / total if total else 0.0,
        "goal_total": total, "goal_completed": completed,
        "goal_completion_rate": completed / total if total else 0.0,
        "goals": goals, "unexpected_goals": unexpected, "subtasks": subtasks,
        "goal_status_counts": goal_counts, "status_counts": goal_counts,
        "subtask_status_counts": sub_counts,
        "total_duration_ms": root_duration, "root_duration_sum_ms": root_duration,
        "mean_duration_ms": round(sum(durations) / len(durations), 2) if durations else 0.0,
        "p95_duration_ms": round(_percentile(durations, 0.95), 2),
        "duration_known_count": len(durations), "duration_unknown_count": total - len(durations),
        "active_duration_ms": clock_total("active_duration_ms"),
        "wait_duration_ms": clock_total("wait_duration_ms"),
        "mean_steps": round(sum(row["steps"] for row in goals) / total, 2) if total else 0.0,
        "step_count": step_count, "model_usage_events": len(usage_events),
        "total_tokens": total_tokens, "usage_unknown_calls": unknown_usage,
        "usage_estimated_calls": estimated_usage, "http_attempts": http_attempts,
        "usage_unknown_attempts": unknown_attempts,
        "http_attempts_unknown_calls": missing_attempt_counts,
        "mean_tokens_per_task": round(sum(row["tokens"] for row in goals) / total) if total else 0,
        "failure_modes": failures,
        "subtask_total": sub_total, "subtask_completed": sub_completed,
        "subtask_completion_rate": sub_completed / sub_total if sub_total else 0.0,
        "subtask_duration_sum_ms": sub_duration,
        "subtask_mean_duration_ms": round(sum(sub_durations) / len(sub_durations), 2) if sub_durations else 0.0,
        "subtask_duration_known_count": len(sub_durations),
        "subtask_duration_unknown_count": sub_total - len(sub_durations),
        "subtask_failure_modes": sub_failures,
        "goal_status": goals[0]["status"] if total == 1 else ("multiple" if total else ""),
        "goal_duration_ms": goals[0]["duration_ms"] if total == 1 else None,
    }


def render_report(
    run_dir: Path | str, write: bool = True,
    expected_cases: Iterable[str | dict[str, Any]] | None = None,
) -> str:
    """Render exactly the same root/subtask summary used by the CLI."""
    run_dir = Path(run_dir)
    events = load_events(run_dir)
    summary = summarize(events, expected_cases=expected_cases)

    def cell(value: Any) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ") if value is not None else "未记录"

    def seconds(value: float | None) -> str:
        return f"{value / 1000:.2f} s" if value is not None else "未记录"

    lines = [
        f"# 运行报告：{run_dir.name}",
        "",
        "## 总览",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 指定/登记 root 任务总数 | {summary['total_tasks']} |",
        f"| 独立验收通过数 | {summary['completed']} |",
        f"| **任务完成率（Goal TCR）** | **{summary['completion_rate']:.1%}** |",
        f"| root 已记录运行墙钟耗时合计 | {seconds(summary['total_duration_ms'])} |",
        f"| 平均耗时（有完整记录的 root） | {seconds(summary['mean_duration_ms']) if summary['duration_known_count'] else '未记录'} |",
        f"| P95 耗时（有完整记录的 root） | {seconds(summary['p95_duration_ms']) if summary['duration_known_count'] else '未记录'} |",
        f"| 耗时未知 root 数 | {summary['duration_unknown_count']} |",
        f"| 活跃时间 | {seconds(summary['active_duration_ms'])} |",
        f"| 等待时间 | {seconds(summary['wait_duration_ms'])} |",
        f"| 平均步数 | {summary['mean_steps']} |",
        f"| 已记录 token（含估算/已知部分） | {summary['total_tokens']} |",
        f"| 平均 token/任务 | {summary['mean_tokens_per_task']} |",
        f"| 用量记录不完整的调用数 | {summary['usage_unknown_calls']} |",
        f"| 使用估算用量的调用数 | {summary['usage_estimated_calls']} |",
        f"| 已记录 HTTP 尝试数 | {summary['http_attempts']} |",
        f"| 用量未知的 HTTP 尝试数 | {summary['usage_unknown_attempts']} |",
        f"| HTTP 尝试次数不可确定的调用数 | {summary['http_attempts_unknown_calls']} |",
        "",
        "耗时合计是各 root 各次运行记录之和，不是并发批次的墙钟时间。成功要求正常结束且有身份匹配的 passed 验收报告。",
        "token 保留服务端记录、估算及已知部分；估算不等于实测，未知不等于零。仅明确标记为 HTTP 的调用计入 HTTP 尝试数；离线调用为零，传输类型或次数缺失则标记不可确定。成功请求此前失败的 HTTP 尝试、最终失败请求的全部 HTTP 尝试均可能有未报告用量；这里只汇总记录，不是完整账单或预算 ledger。",
        "",
        "## 总体目标",
        "",
        f"- Goal TCR：**{summary['goal_completion_rate']:.1%}**（{summary['goal_completed']}/{summary['goal_total']}）",
        f"- 状态分布：{cell(summary['goal_status_counts'])}",
        "",
        "## 失败模式",
        "",
    ]
    if summary["failure_modes"]:
        lines += ["| 原因 | 次数 |", "|---|---|"]
        lines += [f"| {cell(reason)} | {count} |" for reason, count in summary["failure_modes"].items()]
    else:
        lines.append("无失败任务。")

    if summary["subtasks"]:
        lines += [
            "",
            "## 子任务",
            "",
            f"- 子任务完成率：**{summary['subtask_completion_rate']:.1%}**"
            f"（{summary['subtask_completed']}/{summary['subtask_total']}）",
            f"- 状态分布：{cell(summary['subtask_status_counts'])}",
            f"- 子任务耗时相加：{seconds(summary['subtask_duration_sum_ms'])}（含并发及嵌套，不作为 root 墙钟耗时）",
            "",
            "| root | 子任务 | agent | 状态 | 尝试数 | 耗时 | token |",
            "|---|---|---|---|---|---|---|",
        ]
        for row in summary["subtasks"]:
            lines.append("| " + " | ".join(cell(value) for value in (
                row["root_id"] or "未标注", row["task_id"], row["agent"], row["status"],
                row["attempts"], seconds(row["duration_ms"]), row["tokens"],
            )) + " |")
        if summary["subtask_failure_modes"]:
            lines += ["", "失败子任务原因："]
            lines += [f"- {cell(reason)}：{count} 次" for reason, count in summary["subtask_failure_modes"].items()]

    lines += ["", "## 逐任务明细", "", "| root 任务 | 状态 | 尝试数 | 步数 | 工具调用 | 耗时 | token | 报告/原因 |", "|---|---|---|---|---|---|---|---|"]
    for row in summary["goals"]:
        lines.append("| " + " | ".join(cell(value) for value in (
            row["task_id"], row["status"], row["attempts"], row["steps"], row["tool_calls"],
            seconds(row["duration_ms"]), row["tokens"], row["report_id"] or row["error"],
        )) + " |")
    if summary["unexpected_goals"]:
        lines += ["", "未列入指定评测题的 root（不改变 Goal TCR）："]
        lines += [f"- {cell(row['task_id'])}：{cell(row['status'])}" for row in summary["unexpected_goals"]]

    report = "\n".join(lines) + "\n"
    if write:
        (run_dir / "report.md").write_text(report, encoding="utf-8")
    return report
