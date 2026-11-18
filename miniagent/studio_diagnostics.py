"""Read-only incident projection; never repairs or upgrades persisted tasks."""
from __future__ import annotations

from typing import Any


def diagnose(tasks: list[dict], events: list[dict], acceptance: list[dict],
             *, active: bool = False, incomplete: bool = False) -> dict[str, Any]:
    def scope(event):
        return (event.get("task_id") or event.get("root_id"),
                event.get("attempt_id") or event.get("root_attempt_id"),
                event.get("run_id") or event.get("root_run_id"))

    def truncation_resolved(index, event, planning):
        identity = scope(event)
        if not identity[0]:
            return False
        related = [later for later in events[index + 1:] if scope(later)[0] == identity[0]]
        # Older traces can omit attempt/run IDs. Never join an anonymous event
        # to a later plan when other events introduce a conflicting identity.
        if any(identity[part] is None and scope(later)[part] is not None
               for later in related for part in (1, 2)):
            return False
        same = [later for later in related if scope(later) == identity]
        accepted_plan = planning and any(later.get("kind") == "plan" for later in same)
        terminals = [later for later in same
                     if later.get("kind") in {"task_end", "orchestration_end"}]
        recovered = not planning and any(later.get("kind") == "step" and later.get("finish_reason") in {"stop", "tool_calls"}
                        and not later.get("error_class") for later in same)
        return bool(accepted_plan or recovered or (terminals and terminals[-1].get("status") == "completed"))

    roots = {task["task_id"] for task in tasks if task.get("parent_id") is None}
    ends = {event.get("task_id"): event for event in events if event.get("kind") == "task_end"}
    report_by_task = {item.get("task_id"): item.get("report", {}) for item in acceptance}
    cancelled = {task["task_id"] for task in tasks if task.get("status") == "cancelled"}
    failed = {identity for identity, event in ends.items()
              if isinstance(identity, str)
              and identity not in cancelled
              and event.get("status") in {"error", "limit_exceeded", "aborted", "failed"}
              and not (event.get("status") == "failed" and not event.get("execution_error")
                       and report_by_task.get(identity, {}).get("verdict") == "failed")}
    failed.update(task["task_id"] for task in tasks
                  if task.get("status") == "failed" and task.get("task_id") not in ends
                  and report_by_task.get(task["task_id"], {}).get("verdict") not in {"failed", "error"})
    blocked = [{"task_id": event.get("task_id"), "reason": event.get("error", ""),
                "assignee": event.get("assignee", "")}
               for event in events if event.get("kind") == "subtask_end" and event.get("status") == "blocked"]
    config = next((event["configuration"] for event in reversed(events)
                   if event.get("kind") == "studio_configuration"
                   and isinstance(event.get("configuration"), dict)), {})
    issues = []
    for index, event in enumerate(events):
        if event.get("kind") not in {"step", "plan_usage"} or event.get("finish_reason") != "length":
            continue
        planning = event.get("kind") == "plan_usage"
        resolved = truncation_resolved(index, event, planning)
        title = ("此前规划输出被截断，后续已恢复" if planning else "此前模型输出被截断，后续已恢复") if resolved else (
            "规划输出被截断，子任务未调度" if planning else "模型输出被截断，工具未执行")
        message = ("该次规划返回 length；同一执行尝试后续接受了有效计划或完成了任务。" if planning else
                   "该次响应未执行工具；同一执行尝试后续已收到完整响应。") if resolved else (
            "规划模型返回 length，不接受不完整的任务图。" if planning else "模型返回 length，未完成的工具参数没有被执行。")
        issues.append({"code": "output_truncated", "severity": "warning" if resolved else "error",
                       "resolved": resolved, "title": title,
                       "task_id": event.get("task_id"),
                       "step": event.get("step"), "finish_reason": "length",
                       "completion_tokens": event.get("completion_tokens"),
                       "max_output_tokens": event.get("max_output_tokens", config.get("max_output_tokens")),
                       "request_id": event.get("request_id"), "latency_ms": event.get("latency_ms"),
                       "message": message,
                       "suggestion": "已保留历史告警；以本次任务的最终执行和验收记录为准。" if resolved else
                       "本次自动恢复未完成。查看 output_recovery 事件；大文件可分块写入或局部修改，保留的产物可以下载。"})
    for identity in sorted(failed):
        if any(issue.get("task_id") == identity and issue["severity"] == "error" for issue in issues):
            continue
        event = ends.get(identity, {})
        error = event.get("error") or "持久任务记录显示失败。"
        code, title = "execution_failed", "任务执行失败"
        suggestion = "查看该任务的错误事件和工具结果，处理原因后再发起新任务。"
        if event.get("status") == "limit_exceeded":
            if "步数" in error:
                code, title = "step_limit", "已达到执行轮数上限"
                suggestion = "已生成的产物仍然保留。下次任务可在选项中增加执行轮数；这与模型单次输出上限不同。"
            elif "token" in error.lower():
                code, title = "token_budget", "已达到累计 token 预算"
                # Terminal totals cover the whole attempt even when the GUI
                # event preview is bounded. Older traces also have these fields.
                prompt, output = event.get("prompt_tokens"), event.get("completion_tokens")
                if type(prompt) is int and type(output) is int:
                    error += f"。本次累计输入 {prompt:,}、输出 {output:,}，合计 {prompt + output:,} tokens。"
                suggestion = "每轮重发的历史也计入累计用量。新一轮可将累计 token 预算设为 0（不限），继续使用保留的工件。"
                if any(e.get("kind") == "token_budget_preflight" and scope(e) == scope(event) for e in events):
                    title = "剩余 token 预算不足以发起下一轮"
            elif "时间" in error:
                code, title = "time_budget", "已达到任务时间预算"
                suggestion = "下次任务可增加任务时间预算。已有产物和日志仍然保留。"
            elif "T_LOOP" in error or "重复动作" in error:
                code, title = "loop_limit", "检测到重复动作，已停止循环"
                suggestion = "检查重复调用及工具结果；增加 token 或时间上限不能修复没有进展的循环。"
            elif "动作总数" in error:
                code, title = "action_limit", "已达到工具动作总数上限"
                suggestion = "查看动作数量及重复操作；已有产物仍然保留。"
            else:
                code, title = "runtime_limit", "执行受到运行限制"
                suggestion = "查看具体终止原因和配置快照；现有证据不足以归类为输出、轮数或时间限制。"
        if any(e.get("kind") == "context_limit" and e.get("task_id") == identity for e in events):
            code, title = "context_limit", "上下文估计超过当前请求空间"
            suggestion = "旧历史已按完整工具批次裁剪；受保护的指令和最近批次仍过大，需要拆分任务或缩短输入。"
        if any(e.get("kind") == "provider_error" and e.get("code") == "context_length_exceeded"
               and scope(e) == scope(event) and not e.get("recovery") for e in events):
            code, title = "provider_context_limit", "模型服务拒绝了超出窗口的请求"
            suggestion = "检查本轮输入和工具回执大小，以及是否已有缩小请求的重试。"
        issues.append({"code": code, "severity": "error", "task_id": identity,
                       "title": title, "message": error, "suggestion": suggestion})
    for event in events:
        if event.get("kind") == "studio_error":
            issues.append({"code": "studio_error", "severity": "error", "title": "工作室执行异常",
                           "task_id": event.get("task_id"), "message": event.get("message", ""),
                           "error_code": event.get("error_code"), "frames": event.get("frames", []),
                           "suggestion": "导出诊断记录，检查对应模块与异常类型。"})
    root_reports = [item for item in acceptance if item.get("task_id") in roots]
    latest = root_reports[-1] if root_reports else None
    if (latest and latest.get("spec") is None
            and latest.get("report", {}).get("verdict") == "unverified"):
        verification = "not_configured"
    elif latest:
        verification = latest.get("report", {}).get("verdict", "unverified")
    else:
        verification = "pending" if active else "not_recorded"
    if verification == "not_configured":
        issues.append({"code": "no_acceptance", "severity": "warning", "title": "没有配置独立验收",
                       "message": "没有可执行的检查规则，不能确认结果满足目标。这与执行是否报错是两件事。",
                       "suggestion": "单 Agent 可填写验收文件；复杂协作验收目前通过 CLI 的 --acceptance 配置。"})
    elif verification in {"failed", "error"}:
        issues.append({"code": "acceptance_failed" if verification == "failed" else "acceptance_error",
                       "severity": "error", "title": "独立验收未通过" if verification == "failed" else "验收检查异常",
                       "message": latest.get("report", {}).get("reason") or "查看验收报告中的逐项检查结果。",
                       "suggestion": "根据验收报告修正产物或检查器，再发起新任务。"})
    starts = {}

    for event in events:
        kind = event.get("kind")
        planning = kind in {"plan_start", "plan_usage"} or (kind == "provider_error" and event.get("step") is None)
        identity = (scope(event), "planning" if planning else "step",
                    event.get("call_id") if planning else event.get("step"))
        if kind in {"step_start", "plan_start"}:
            starts[identity] = event
        elif kind in {"step", "plan_usage", "provider_error"}:
            starts.pop(identity, None)
        elif kind in {"task_end", "subtask_end", "orchestration_end"}:
            starts = {key: value for key, value in starts.items() if scope(value) != scope(event)}
    current = list(starts.values()) if active else []
    delivered = (bool(roots) and verification == "passed"
                 and all(t.get("status") == "completed" for t in tasks if t["task_id"] in roots))
    if delivered:
        for issue in issues:
            if issue.get("task_id") and issue["task_id"] not in roots and issue["severity"] == "error":
                issue["severity"] = "warning"
                issue["suggestion"] = "子任务的原始问题保留在记录中，项目最终交付已通过独立验收。"
    has_error = bool((failed & roots if delivered else failed) or any(item["severity"] == "error" and item["code"] in {"studio_error", "output_truncated"}
                                 for item in issues))
    root_finished = any(identity in ends for identity in roots)
    execution = ("running" if active else "cancelled" if roots & cancelled else "failed" if has_error else "blocked" if blocked and not delivered
                 else "finished" if root_finished else "unknown")
    return {"execution_status": execution, "acceptance_status": verification,
            "issues": issues, "blocked_tasks": blocked,
            # step_start covers Reason -> Act -> Observe, including tool time;
            # it is not a provider-only request span.
            "current_activity": [{"phase": "planning" if item["kind"] == "plan_start" else item.get("phase", "agent_step"),
                                  "task_id": item.get("task_id") or item.get("root_id"),
                                  "step": item.get("step"), "started_at": item.get("iso"),
                                  "started_ts": item.get("ts")}
                                 for item in current],
            "configuration": config, "incomplete": incomplete}
