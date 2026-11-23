"""Live measurements for one task: time, steps, tokens, estimated cost, slow tools and alerts.

The quantities follow the OpenTelemetry GenAI conventions: the duration of each
model operation (``gen_ai.client.operation.duration``), inference and tool calls
per agent (``gen_ai.invoke_agent.inference_calls`` / ``tool_calls``), and input,
cached and output tokens per request, as the OpenAI Agents SDK keeps per-request
usage entries. Nothing is exported; the meter feeds the chat's live bar, the task
record and soft budgets that warn the lead.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

# USD per million tokens (DeepSeek pricing page, Nov 2026): cache hit, cache miss, output.
PRICES = {"deepseek-flash": (0.006, 0.30, 1.20), "deepseek-v4-pro": (0.044, 1.32, 3.96)}
PEAK_UTC = ((1, 4), (6, 10))  # weekday hours (UTC) at full price; other times are half price

SLOW_TOOL_SECONDS = 60
QUIET_SECONDS = 180
STEP_WARNING = 0.8
BUDGET_MARKS = (0.5, 0.8, 1.0)


def price_factor(at: float) -> float:
    moment = datetime.fromtimestamp(at, timezone.utc)
    peak = moment.weekday() < 5 and any(start <= moment.hour < end for start, end in PEAK_UTC)
    return 1.0 if peak else 0.5


def call_cost(model: str, prompt: int, cached: int, output: int, at: float) -> float:
    hit, miss, out = PRICES.get(model, PRICES["deepseek-flash"])
    return (cached * hit + max(prompt - cached, 0) * miss + output * out) / 1e6 * price_factor(at)


class TaskMeter:
    """Thread safe: parallel hand-offs feed it from several threads."""

    def __init__(self, *, budget_minutes: float = 0, budget_tokens: int = 0, elapsed: float = 0.0,
                 clock: Callable[[], float] = time.time, notify: Callable[[str], None] | None = None) -> None:
        self.clock = clock
        self.started = clock() - elapsed
        self.budget_minutes = budget_minutes
        self.budget_tokens = budget_tokens
        self.notify = notify
        self.lock = threading.Lock()
        self.agents: dict[str, dict[str, Any]] = {}
        self.open_tools: dict[str, tuple[str, str, float]] = {}
        self.slowest: list[dict[str, Any]] = []
        self.last_progress = self.started
        self.cost = 0.0
        self.crossed: set[tuple[str, float]] = set()

    def _agent(self, name: str) -> dict[str, Any]:
        return self.agents.setdefault(name, {"steps": 0, "max_steps": 0, "model_s": 0.0, "model_calls": 0,
                                             "tool_s": 0.0, "tool_calls": 0, "prompt": 0, "cached": 0,
                                             "output": 0, "runs": 0, "run_step": 0})

    # -- input ------------------------------------------------------------
    def record(self, event: dict) -> None:
        kind, name = event.get("type"), event.get("agent") or "?"
        now = self.clock()
        warnings = []
        with self.lock:
            agent = self._agent(name)
            if kind in ("start", "resume"):
                agent["runs"] += 1
            elif kind == "step":
                agent["steps"] += 1
                agent["run_step"] = event.get("step") or agent["run_step"] + 1
                agent["max_steps"] = event.get("max_steps") or agent["max_steps"]
                self.last_progress = now
            elif kind == "end":
                agent["run_step"] = 0
            elif kind == "model_call":
                agent["model_s"] += event.get("seconds", 0.0)
                agent["model_calls"] += 1
                prompt, cached, output = (event.get("prompt_tokens", 0), event.get("cached_tokens", 0),
                                          event.get("output_tokens", 0))
                agent["prompt"] += prompt
                agent["cached"] += cached
                agent["output"] += output
                self.cost += call_cost(event.get("model", ""), prompt, cached, output, now)
                self.last_progress = now
            elif kind == "tool_call":
                self.open_tools[event.get("call_id", "")] = (name, event.get("tool", "?"), now)
            elif kind == "tool_result":
                opened = self.open_tools.pop(event.get("call_id", ""), None)
                seconds = event.get("seconds")
                if seconds is None and opened:
                    seconds = now - opened[2]
                if event.get("tool") not in ("delegate_task", "review_loop") and seconds is not None:
                    agent["tool_s"] += seconds
                    agent["tool_calls"] += 1
                    self.slowest.append({"agent": name, "tool": event.get("tool"), "seconds": round(seconds, 1)})
                    self.slowest = sorted(self.slowest, key=lambda r: -r["seconds"])[:8]
                self.last_progress = now
            warnings = self._budget_marks(now)
        for text in warnings:
            if self.notify:
                self.notify(text)

    def _budget_marks(self, now: float) -> list[str]:
        notes = []
        usage = {"minutes": ((now - self.started) / 60, self.budget_minutes),
                 "tokens": (sum(a["prompt"] + a["output"] for a in self.agents.values()), self.budget_tokens)}
        for name, (used, limit) in usage.items():
            if not limit:
                continue
            for mark in BUDGET_MARKS:
                if used >= limit * mark and (name, mark) not in self.crossed:
                    self.crossed.add((name, mark))
                    shown = f"{used:.0f} of {limit:.0f} minutes" if name == "minutes" else f"{used:,} of {limit:,} tokens"
                    if mark < 1:
                        notes.append(f"[Studio budget notice] This task has used {int(mark * 100)}% of its soft "
                                     f"{name} budget ({shown}). Keep the remaining work focused on what the "
                                     "requester needs; skip optional polish and repeated checks.")
                    else:
                        notes.append(f"[Studio budget notice] The soft {name} budget is used up ({shown}). Finish "
                                     "with what the team has, verify the essential result once, and report what "
                                     "is still open. Nothing is stopped for you.")
        return notes

    # -- output -----------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        now = self.clock()
        with self.lock:
            agents = [{"agent": name, **{k: (round(v, 1) if isinstance(v, float) else v) for k, v in data.items()}}
                      for name, data in self.agents.items()]
            prompt = sum(a["prompt"] for a in self.agents.values())
            cached = sum(a["cached"] for a in self.agents.values())
            output = sum(a["output"] for a in self.agents.values())
            alerts = []
            for agent, tool, started in self.open_tools.values():
                if tool not in ("delegate_task", "review_loop") and now - started >= SLOW_TOOL_SECONDS:
                    alerts.append({"kind": "slow_tool", "agent": agent, "tool": tool, "seconds": round(now - started)})
            if now - self.last_progress >= QUIET_SECONDS:
                alerts.append({"kind": "quiet", "seconds": round(now - self.last_progress)})
            for name, data in self.agents.items():
                if data["max_steps"] and data["run_step"] >= STEP_WARNING * data["max_steps"]:
                    alerts.append({"kind": "step_budget", "agent": name, "steps": data["run_step"],
                                   "max_steps": data["max_steps"]})
            budget = {}
            if self.budget_minutes:
                budget["minutes"] = {"used": round((now - self.started) / 60, 1), "limit": self.budget_minutes}
            if self.budget_tokens:
                budget["tokens"] = {"used": prompt + output, "limit": self.budget_tokens}
            return {"elapsed_s": round(now - self.started, 1),
                    "model_s": round(sum(a["model_s"] for a in self.agents.values()), 1),
                    "tool_s": round(sum(a["tool_s"] for a in self.agents.values()), 1),
                    "model_calls": sum(a["model_calls"] for a in self.agents.values()),
                    "steps": sum(a["steps"] for a in self.agents.values()),
                    "tokens": {"prompt": prompt, "cached": cached, "output": output},
                    "cost_usd": round(self.cost, 4), "agents": agents, "slowest_tools": list(self.slowest),
                    "alerts": alerts, "budget": budget}
