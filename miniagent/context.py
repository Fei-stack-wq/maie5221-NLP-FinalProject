"""Context management: token estimates, tool-output pruning and summary compaction.

Design (adapted, see docs/architecture.md for pinned upstream links):

* Pi coding-agent ``compaction.ts`` — compact only when the request approaches
  a threshold, keep the most recent ``keep_recent_tokens`` verbatim, never cut
  between an assistant tool call and its tool results, and replace the older
  span with one structured checkpoint summary (Goal / Constraints / Progress /
  Key Decisions / Next Steps / Critical Context). A previous summary is
  updated rather than summarized again.
* OpenCode / DeepSeek-harness tool-result pruning — before paying for a
  summary, shrink large *old* tool outputs to a short head. Only done when the
  threshold is crossed, so the request prefix stays stable (prompt caching).

Differences from Pi: the system prompt and the original task message are
pinned (never summarized), because the team protocol depends on them, and a
failed summary falls back to deterministic pruning instead of aborting.

The old runtime projected only the last four turns into a "compact" request.
The lead then forgot what it had just read and re-read the same files 8,586
times in one run. This module never drops context silently: older content is
either still present or represented by a summary the model can rely on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

SUMMARY_PREFIX = "[Context checkpoint — summary of earlier work in this assignment]\n"

SUMMARIZATION_PROMPT = """The messages above are an agent's working conversation. Write a structured context checkpoint that the same agent will use to continue the work without the original messages.

Use this EXACT format:

## Goal
[What the agent was asked to accomplish]

## Constraints & Preferences
- [Requirements, user decisions, preferences; or "(none)"]

## Progress
### Done
- [x] [Completed work — name exact files written and checks that passed]
### In Progress
- [ ] [Current work]
### Blocked
- [Problems, if any]

## Key Decisions
- **[Decision]**: [rationale]

## Facts Already Established
- [Findings from files, commands, web sources (with URLs), test results — so they need not be looked up again]

## Next Steps
1. [What to do next]

Be concise but preserve exact file paths, URLs, numbers, error messages and the content of user answers."""

UPDATE_PROMPT = """The messages above are NEW messages. Update the existing checkpoint in <previous-summary> tags: keep everything still relevant, move finished items to Done, add new facts, decisions and next steps. Use the same EXACT format as the previous summary.

<previous-summary>
{previous}
</previous-summary>"""


def estimate_text_tokens(text: str) -> int:
    """Rough token estimate that does not undercount CJK text."""
    if not text:
        return 0
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    other = len(text) - ascii_chars
    return int(ascii_chars / 4 + other * 0.75) + 1


def message_tokens(message: dict[str, Any]) -> int:
    total = 4 + estimate_text_tokens(message.get("content") or "")
    if message.get("reasoning_content"):
        total += estimate_text_tokens(message["reasoning_content"])
    for call in message.get("tool_calls") or ():
        function = call.get("function", {})
        total += 8 + estimate_text_tokens(function.get("name", "")) + estimate_text_tokens(function.get("arguments", ""))
    return total


def estimate_tokens(messages: list[dict[str, Any]], tools: list[dict] | None = None) -> int:
    total = sum(message_tokens(m) for m in messages)
    if tools:
        total += estimate_text_tokens(json.dumps(tools, ensure_ascii=False))
    return total


def _valid_cut(messages: list[dict[str, Any]], index: int) -> bool:
    """A cut may start at a user message or an assistant message, never at a tool result."""
    return messages[index].get("role") in {"user", "assistant"}


def find_cut_point(messages: list[dict[str, Any]], start: int, keep_recent_tokens: int) -> int:
    """Index of the first message to keep verbatim; ``start`` means nothing to compact.

    Walk back from the newest message until ``keep_recent_tokens`` are covered,
    then cut at the closest valid boundary at or after that point. If the
    trailing tool results alone exceed the budget, keep their assistant call
    instead (Pi ``findCutPoint``), so a tool batch is never split.
    """
    accumulated = 0
    for index in range(len(messages) - 1, start - 1, -1):
        accumulated += message_tokens(messages[index])
        if accumulated >= keep_recent_tokens:
            for candidate in range(index, len(messages)):
                if _valid_cut(messages, candidate):
                    return max(candidate, start)
            for candidate in range(index - 1, start - 1, -1):
                if _valid_cut(messages, candidate):
                    return candidate
            return start
    return start


def prune_tool_outputs(messages: list[dict[str, Any]], *, protect_from: int, keep_chars: int = 1200) -> int:
    """Shrink large tool results before ``protect_from``. Returns characters removed."""
    removed = 0
    for message in messages[:protect_from]:
        if message.get("role") != "tool":
            continue
        content = message.get("content") or ""
        if len(content) <= keep_chars * 2:
            continue
        head = content[:keep_chars]
        dropped = len(content) - keep_chars
        message["content"] = (head + f"\n…[older tool output trimmed: {dropped} more characters. "
                               "Re-run the tool if the details are needed again.]")
        removed += dropped
    return removed


def _transcript(messages: list[dict[str, Any]], limit_chars: int = 400_000) -> str:
    """Render messages as plain text for the summarizer (no reasoning, bounded)."""
    lines = []
    for message in messages:
        role = message.get("role")
        content = message.get("content") or ""
        if role == "tool":
            if len(content) > 6000:
                content = content[:4000] + "\n…\n" + content[-1500:]
            lines.append(f"[tool result]\n{content}")
        elif role == "assistant":
            calls = []
            for call in message.get("tool_calls") or ():
                function = call.get("function", {})
                arguments = function.get("arguments", "")
                if len(arguments) > 1500:
                    arguments = arguments[:1500] + "…"
                calls.append(f"{function.get('name')}({arguments})")
            text = content.strip()
            lines.append("[assistant]" + (f"\n{text}" if text else "") + ("".join(f"\n→ call {c}" for c in calls)))
        else:
            lines.append(f"[{role}]\n{content}")
    text = "\n\n".join(lines)
    if len(text) > limit_chars:
        text = text[: limit_chars // 3] + "\n\n…[middle omitted]…\n\n" + text[-(2 * limit_chars) // 3:]
    return text


@dataclass
class CompactionResult:
    applied: bool
    method: str = ""
    before_tokens: int = 0
    after_tokens: int = 0
    usage: Any = None
    error: str = ""


@dataclass
class ContextManager:
    """Owns an agent's message list and keeps it under ``compact_at_tokens``."""

    compact_at_tokens: int
    keep_recent_tokens: int
    pinned: int = 2
    """Leading messages never compacted (system prompt and the original task)."""
    summary: str = ""
    compactions: int = 0
    last_reported_prompt_tokens: int = 0
    reported_at_length: int = 0
    events: list[dict] = field(default_factory=list)
    anchors: list[dict[str, Any]] = field(default_factory=list)
    """Messages kept verbatim through compaction (the current assignment of a continued session)."""

    def anchor(self, message: dict[str, Any]) -> None:
        """Keep ``message`` verbatim; it replaces any earlier anchor."""
        self.anchors = [message]

    def _anchored(self, message: dict[str, Any]) -> bool:
        # Equality as well as identity: a paused run restores its messages from JSON.
        return any(message is kept or message == kept for kept in self.anchors)

    def note_usage(self, messages: list[dict[str, Any]], prompt_tokens: int, length_at_request: int) -> None:
        self.last_reported_prompt_tokens = prompt_tokens
        self.reported_at_length = length_at_request

    def current_tokens(self, messages: list[dict[str, Any]], tools: list[dict] | None = None) -> int:
        """Last reported prompt size plus an estimate of messages appended since."""
        if self.last_reported_prompt_tokens and self.reported_at_length <= len(messages):
            return self.last_reported_prompt_tokens + estimate_tokens(messages[self.reported_at_length:])
        return estimate_tokens(messages, tools)

    def maybe_compact(self, messages: list[dict[str, Any]], tools: list[dict] | None,
                      summarize: Callable[[list[dict[str, Any]]], tuple[str, Any]] | None) -> CompactionResult:
        before = self.current_tokens(messages, tools)
        if before <= self.compact_at_tokens:
            return CompactionResult(False, before_tokens=before, after_tokens=before)
        start = min(self.pinned, len(messages))
        if self.summary and start < len(messages) and (messages[start].get("content") or "").startswith(SUMMARY_PREFIX):
            start += 1  # the existing summary message is regenerated, not summarized again
        cut = find_cut_point(messages, start, self.keep_recent_tokens)
        # Step 1: prune large old tool outputs (cheap, deterministic).
        prune_tool_outputs(messages, protect_from=cut)
        self._reset_usage_anchor()
        after_prune = estimate_tokens(messages, tools)
        if after_prune <= int(self.compact_at_tokens * 0.6) or cut <= start or summarize is None:
            self.compactions += 1
            return CompactionResult(True, "prune", before, after_prune)
        # Step 2: summarize the span [start, cut) into a checkpoint message. Anchored
        # messages (the assignment being worked on) stay verbatim after the summary.
        kept = [m for m in messages[start:cut] if self._anchored(m)]
        span = [m for m in messages[start:cut] if not self._anchored(m)]
        if not span:
            self.compactions += 1
            return CompactionResult(True, "prune", before, after_prune)
        previous = self.summary
        prompt_messages = [
            {"role": "system", "content": "You condense an agent's working history into a precise checkpoint."},
            {"role": "user", "content": _transcript(span)},
            {"role": "user", "content": UPDATE_PROMPT.format(previous=previous) if previous else SUMMARIZATION_PROMPT},
        ]
        try:
            text, usage = summarize(prompt_messages)
        except Exception as exc:  # noqa: BLE001 - compaction must never kill the run
            self.compactions += 1
            return CompactionResult(True, "prune", before, after_prune, error=f"{type(exc).__name__}: {exc}")
        text = (text or "").strip()
        if not text:
            self.compactions += 1
            return CompactionResult(True, "prune", before, after_prune, error="empty summary")
        self.summary = text
        summary_message = {"role": "user", "content": SUMMARY_PREFIX + text}
        pinned = messages[:min(self.pinned, len(messages))]
        messages[:] = [*pinned, summary_message, *kept, *messages[cut:]]
        self._reset_usage_anchor()
        self.compactions += 1
        return CompactionResult(True, "summary", before, estimate_tokens(messages, tools), usage=usage)

    def _reset_usage_anchor(self) -> None:
        self.last_reported_prompt_tokens = 0
        self.reported_at_length = 0
