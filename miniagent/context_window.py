"""Bounded request projection without rewriting the append-only execution history.

This layer selects host-verified boundaries; RollingCompactor may replace the
omission notice with a semantic summary for those same ranges. Complete
assistant/tool batches are indivisible; every system/user message and the latest
two assistant batches stay available. Provider-reported usage anchors exact
request prefixes and complete assistant messages. Unknown new input uses a
conservative byte estimate, without a model-specific tokenizer dependency.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any


class ContextLimitError(ValueError):
    """Protected messages and the output reservation cannot fit in the window."""


class TaskTokenLimitError(ValueError):
    """Even the smallest valid request does not fit the remaining task allocation."""


@dataclass(frozen=True)
class RequestProjection:
    messages: list[dict[str, Any]]
    estimated_input_tokens: int
    original_input_tokens: int
    input_limit: int
    omitted_groups: int = 0
    omitted_messages: int = 0
    estimate_source: str = "utf8_bytes_upper_estimate"
    # Half-open indices into the original (unfiltered) history, never model supplied.
    omitted_spans: tuple[tuple[int, int], ...] = ()
    notice_index: int | None = None


@dataclass(frozen=True)
class UsageAnchor:
    """Measured previous input + output, bound to exact messages and schemas."""

    messages: list[dict[str, Any]]
    tool_schemas: list[dict[str, Any]]
    measured_tokens: int


def estimate_tokens(value: Any) -> int:
    """Conservative byte estimate, explicitly not measured provider usage."""
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def project_history(
    history: list[dict[str, Any]], *, tool_schemas: list[dict[str, Any]],
    context_window_tokens: int, output_reserve: int,
    excluded_ranges: list[tuple[int, int]] | None = None,
    usage_anchor: UsageAnchor | None = None,
    reported_assistant_tokens: dict[int, tuple[dict[str, Any], int]] | None = None,
    task_tokens_left: int | None = None, steps_left: int | None = 1,
    replay_input_target: int | None = None,
) -> RequestProjection:
    """Build a valid request view, retaining original history and call ID pairs.

    ``excluded_ranges`` are whole failed responses and their synthetic skipped
    results, recorded by the runtime when it schedules a recovery. Never use
    this to omit a response whose actions may already have executed.
    """
    margin = max(256, context_window_tokens // 100)
    input_limit = context_window_tokens - output_reserve - margin
    limits_description = (f"context_window_tokens={context_window_tokens}, "
                          f"max_output_tokens={output_reserve}, "
                          f"input_limit_tokens={max(0, input_limit)}")
    if input_limit <= 0:
        raise ContextLimitError(
            f"上下文窗口不足以预留本轮输出及协议空间（{limits_description}）；"
            "请降低单次输出上限或增大上下文窗口。"
        )
    # Reserve at least one response token. The lookahead is a soft target for
    # replay cost, never a ceiling on a single substantive read or generation.
    hard_limit = input_limit if task_tokens_left is None else min(input_limit, task_tokens_left - 1)
    replay_target = hard_limit if task_tokens_left is None else min(
        hard_limit, task_tokens_left // (48 if steps_left is None else max(1, min(steps_left, 48)))
    )
    if replay_input_target is not None:
        replay_target = min(replay_target, max(1, replay_input_target))
    excluded = excluded_ranges or []
    visible = []
    original_indices = []
    measured_costs: dict[int, int] = {}
    for index, message in enumerate(history):
        if any(start <= index < end for start, end in excluded):
            continue
        visible.append(message)
        original_indices.append(index)
        measured = (reported_assistant_tokens or {}).get(index)
        if measured and message == measured[0] and message.get("role") == "assistant":
            # Completion usage includes thinking and function-call output;
            # retain a framing allowance for the next request's message form.
            measured_costs[id(message)] = measured[1] + 64 + len(message.get("tool_calls") or []) * 32
    # Group each assistant and ALL of its tool replies. Never leave an orphan
    # result or remove only part of a parallel tool-call batch.
    groups: list[list[dict[str, Any]]] = []
    removable: list[int] = []
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(visible):
        start_index = i
        message = visible[i]
        role = message.get("role")
        if role == "tool":
            raise ContextLimitError("执行历史包含未配对的工具结果，无法安全构造上下文。")
        group = [message]
        calls = message.get("tool_calls") if role == "assistant" else None
        if calls:
            expected = [call.get("id") for call in calls]
            if None in expected or len(set(expected)) != len(expected):
                raise ContextLimitError("执行历史包含无效的工具调用身份，无法安全构造上下文。")
            received: list[str] = []
            for _ in calls:
                i += 1
                if i >= len(visible) or visible[i].get("role") != "tool":
                    raise ContextLimitError("执行历史包含未结束的工具调用批次，无法安全构造上下文。")
                received.append(visible[i].get("tool_call_id"))
                group.append(visible[i])
            if set(received) != set(expected) or len(set(received)) != len(received):
                raise ContextLimitError("执行历史的工具调用与结果不匹配，无法安全构造上下文。")
        if role == "assistant":
            removable.append(len(groups))
        groups.append(group)
        spans.append((original_indices[start_index], original_indices[i] + 1))
        i += 1

    # Tool schemas and a framing allowance occupy input space too.
    fixed_cost = estimate_tokens(tool_schemas) + 512

    costs = dict(measured_costs)

    def message_cost(message: dict[str, Any]) -> int:
        return costs.get(id(message), 0) or estimate_tokens(message) + 16

    for message in visible:
        if id(message) not in costs:
            costs[id(message)] = estimate_tokens(message) + 16

    def size(messages: list[dict[str, Any]]) -> tuple[int, str]:
        if (usage_anchor is not None and usage_anchor.tool_schemas == tool_schemas
                and len(messages) >= len(usage_anchor.messages)
                and messages[:len(usage_anchor.messages)] == usage_anchor.messages):
            # Previous prompt usage already includes tool schemas. Previous
            # completion usage covers the appended assistant, including its
            # reasoning. Count only new tool/user messages after that prefix.
            return (usage_anchor.measured_tokens + 256
                    + sum(message_cost(m) for m in messages[len(usage_anchor.messages):]),
                    "provider_usage_anchor")
        has_measured = any(id(message) in measured_costs for message in messages)
        return (sum(message_cost(message) for message in messages) + fixed_cost + 2,
                "reported_completion_and_bytes" if has_measured else "utf8_bytes_upper_estimate")

    original, original_source = size(visible)
    if original <= replay_target:
        return RequestProjection(copy.deepcopy(visible), original, original, hard_limit,
                                 estimate_source=original_source)
    dropped: set[int] = set()
    omitted_messages = 0
    messages = visible
    best = visible
    best_cost, best_source, best_omitted, best_count = original, original_source, 0, 0
    best_spans = ()
    best_notice = None
    paths = {}
    # Preserve recent actions and actual human responses even across many rounds.
    for group_index in removable[:-2]:
        calls = groups[group_index][0].get("tool_calls") or []
        if any(call["function"]["name"] == "ask_human" for call in calls):
            continue
        for call in calls:
            function = call["function"]
            if function["name"] in {"read_file", "read_lines", "read_source", "search_files", "write_file", "edit_file", "append_file", "delete_file"}:
                try:
                    path = json.loads(function["arguments"]).get("path")
                except (AttributeError, ValueError, TypeError):
                    continue
                if isinstance(path, str) and len(path) <= 160:
                    paths[path] = None
        dropped.add(group_index)
        omitted_messages += len(groups[group_index])
        notice = {
            "role": "user",
            "content": (
                f"[上下文整理] 为控制历史重发开销，本次请求省略了较早的 {len(dropped)} 组完整交互"
                f"（{omitted_messages} 条消息）。这只是历史省略，不是内容摘要；"
                "原始执行历史未改写，已执行的动作不会因此撤销。"
                "继续当前分工并参考已有工作记录。若确实缺少某段原文，可用 search_files 定位定义、read_lines 查看对应行，再编辑或运行检查。"
                + (" 历史调用涉及的文件路径：" + json.dumps(list(paths)[-12:], ensure_ascii=False) if paths else "")
            ),
        }
        messages = []
        inserted = False
        for index, group in enumerate(groups):
            if index in dropped:
                if not inserted:
                    notice_index = len(messages)
                    messages.append(notice)
                    inserted = True
            else:
                messages.extend(group)
        estimated, estimate_source = size(messages)
        if estimated < best_cost:
            best, best_cost, best_source = messages, estimated, estimate_source
            best_omitted, best_count = len(dropped), omitted_messages
            best_spans = tuple(spans[g] for g in sorted(dropped))
            best_notice = notice_index
        if estimated <= replay_target:
            break
    if best_cost <= hard_limit:
        return RequestProjection(copy.deepcopy(best), best_cost, original, hard_limit,
                                 best_omitted, best_count, best_source, best_spans, best_notice)
    if task_tokens_left is not None and hard_limit < input_limit:
        raise TaskTokenLimitError(
            f"累计 token 预算剩余 {task_tokens_left}，保留用户要求和近期工具结果后"
            f"预计最少需要 {best_cost} 输入 tokens，另需预留输出。未发起本轮调用。"
        )
    raise ContextLimitError(
        "按当前估算，保留系统指令、用户要求及最近工具批次所需空间仍超出输入预留"
        f"（{limits_description}, protected_input_estimate={size(messages)[0]}, "
        f"estimate_source={size(messages)[1]}）；"
        "已停止本次调用。请降低单次输出预留、缩小任务输入或拆分任务。"
    )
