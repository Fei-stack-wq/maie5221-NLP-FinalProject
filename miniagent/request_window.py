"""Fit local requests to model capacity, including large tool replies.

Uses Pi's separate compaction headroom and output ceiling, and DeepSeek Harness's
head/tail pruning of tool evidence. Raw history stays available to the host.
"""
from __future__ import annotations

from .context_window import ContextLimitError, TaskTokenLimitError, project_history


def output_headroom(window, ceiling):
    # Pi uses 16K as compaction headroom. Scale that threshold for smaller
    # configured windows. The provider may still use the available output space.
    return min(ceiling, 16_384, max(1, window // 8))


def fit_history(history, *, window, ceiling, available, replay_target=None, **options):
    headroom = output_headroom(window, ceiling)
    def project(messages, target=replay_target):
        return project_history(messages, context_window_tokens=window, output_reserve=headroom,
                               task_tokens_left=available, replay_input_target=target, **options)
    try:
        view = project(history)
        if replay_target is None and view.omitted_groups and view.original_input_tokens > (
                window - max(256, window // 100) - headroom):
            # After crossing the physical watermark, free useful working space
            # at once. Pi targets a retained recent tail of about 20K tokens.
            view = project(history, min(view.input_limit, 20_000))
        if replay_target is not None and view.estimated_input_tokens > replay_target:
            # The server can identify overflow even when our estimate fits.
            # Shrink protected tool bodies too on the one recovery attempt.
            smaller = prune_tool_results(history, replay_target)
            if smaller is not history:
                revised = project(smaller)
                if revised.estimated_input_tokens < view.estimated_input_tokens:
                    return revised, True
        return view, False
    except (ContextLimitError, TaskTokenLimitError):
        # The last two tool batches can be protected by compaction. Shorten
        # large receipts in the request view only, preserving complete call IDs.
        capacity = window - max(256, window // 100) - headroom
        if available is not None:
            capacity = min(capacity, available)
        revised = prune_tool_results(history, capacity)
        if revised is history:
            raise
    return project(revised), True


def prune_tool_results(history, capacity):
    calls = {}
    for message in history:
        for call in message.get("tool_calls") or []:
            calls[call.get("id")] = call.get("function", {}).get("name")
    editable = [i for i, m in enumerate(history) if m.get("role") == "tool"
                and isinstance(m.get("content"), str) and calls.get(m.get("tool_call_id"))
                and calls[m["tool_call_id"]] != "ask_human"]
    result = list(history)
    # The excerpts share at most half the available space when many tool
    # results arrive together. Skip short replies and keep the original objects.
    cap = max(128, min(8192, capacity // max(2, 2 * len(editable))))
    for i in editable:
        raw = history[i]["content"].encode()
        if len(raw) <= cap:
            continue
        marker = f"\n[工具结果节选，原始 {len(raw)} 字节；保留首尾，完整回执见运行记录。]\n".encode()
        half = max(1, (cap - len(marker)) // 2)
        result[i] = {**history[i], "content": (raw[:half].decode("utf-8", "ignore") + marker.decode()
                                               + raw[-half:].decode("utf-8", "ignore"))}
    return result if any(result[i] is not history[i] for i in editable) else history
