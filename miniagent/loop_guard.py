"""Stuck-loop detection for one agent run.

Adapted from OpenHands ``StuckDetector`` (software-agent-sdk, pinned in
docs/architecture.md). The same thresholds are used:

* identical action **and** identical observation 4 times in a row;
* identical action failing with the identical error 3 times (nudge), then
  once more (stuck);
* an A/B/A/B… alternating pattern over 6 actions.

Added for this project: a *re-reading* detector. The old runtime failed with
an amnesia loop — every call was slightly different (other line ranges, other
greps), so no identical-action rule fired, but almost every observation was
content the agent had already seen. If most of the recent observations are
exact duplicates of earlier ones, the agent is told so, then stopped.

The guard never kills a run abruptly: the first detection produces a nudge
that is injected into the conversation; a detection that persists after the
nudge returns ``stuck`` and the agent loop asks the model for its final
report without tools.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass, field

ACTION_OBSERVATION = 4
ACTION_ERROR = 3
ALTERNATING = 6
REREAD_WINDOW = 12
REREAD_NUDGE = 8
REREAD_STUCK_WINDOW = 16
REREAD_STUCK = 13


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def action_key(tool: str, arguments: dict) -> str:
    try:
        canonical = json.dumps(arguments, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        canonical = repr(arguments)
    return tool + ":" + _digest(canonical)


@dataclass
class Verdict:
    nudge: str = ""
    stuck: bool = False
    reason: str = ""


@dataclass
class LoopGuard:
    history: deque = field(default_factory=lambda: deque(maxlen=64))
    seen_observations: set = field(default_factory=set)
    nudged: set = field(default_factory=set)
    nudge_step: int | None = None
    steps_since_nudge: int = 0

    def record(self, tool: str, arguments: dict, ok: bool, observation: str) -> None:
        obs = _digest(observation)
        # Waiting on a running process legitimately repeats the same answer.
        duplicate = obs in self.seen_observations and tool != "poll_process"
        self.seen_observations.add(obs)
        self.history.append((action_key(tool, arguments), ok, obs, duplicate, tool))

    def reset(self) -> None:
        """A new user message starts a fresh window (OpenHands: events since last user message)."""
        self.history.clear()
        self.nudged.clear()
        self.nudge_step = None
        self.steps_since_nudge = 0

    # ------------------------------------------------------------------
    def check(self) -> Verdict:
        reason = self._pattern()
        if not reason:
            if self.nudge_step is not None:
                self.steps_since_nudge += 1
                if self.steps_since_nudge > 6:  # recovered for a while
                    self.nudge_step = None
            return Verdict()
        kind = reason.split(":", 1)[0]
        if kind not in self.nudged:
            self.nudged.add(kind)
            self.nudge_step = 0
            self.steps_since_nudge = 0
            return Verdict(nudge=self._message(kind), reason=reason)
        return Verdict(stuck=True, reason=reason)

    def _pattern(self) -> str:
        items = list(self.history)
        if len(items) >= ACTION_OBSERVATION:
            last = items[-ACTION_OBSERVATION:]
            if len({(a, o) for a, _, o, _, _ in last}) == 1:
                return "repeat:" + last[-1][4]
        if len(items) >= ACTION_ERROR:
            last = items[-ACTION_ERROR:]
            if all(not ok for _, ok, _, _, _ in last) and len({(a, o) for a, _, o, _, _ in last}) == 1:
                return "error:" + last[-1][4]
        if len(items) >= ALTERNATING:
            last = items[-ALTERNATING:]
            even = {(a, o) for a, _, o, _, _ in last[0::2]}
            odd = {(a, o) for a, _, o, _, _ in last[1::2]}
            if len(even) == 1 and len(odd) == 1 and even != odd:
                return "alternating:" + last[-1][4]
        if len(items) >= REREAD_STUCK_WINDOW:
            window = items[-REREAD_STUCK_WINDOW:]
            if sum(1 for *_, dup, _ in window if dup) >= REREAD_STUCK and "reread" in self.nudged:
                return "reread:" + window[-1][4]
        if len(items) >= REREAD_WINDOW:
            window = items[-REREAD_WINDOW:]
            if sum(1 for *_, dup, _ in window if dup) >= REREAD_NUDGE and "reread" not in self.nudged:
                return "reread:" + window[-1][4]
        return ""

    @staticmethod
    def _message(kind: str) -> str:
        common = ("Stop and reconsider. If you have enough information, finish now with your final report. "
                  "If something is genuinely blocking you, say exactly what and finish.")
        return {
            "repeat": "[Loop guard] You have made the same tool call with the same result 4 times in a row. "
                      "Repeating it will not produce anything new. " + common,
            "error": "[Loop guard] The same tool call has failed with the same error 3 times. Read the error, "
                     "change the arguments or the approach — do not repeat the call. " + common,
            "alternating": "[Loop guard] You are alternating between the same two actions without progress. " + common,
            "reread": "[Loop guard] Most of your recent tool results repeat content you already received earlier "
                      "in this run. Use what you already know instead of looking it up again. " + common,
        }[kind]
