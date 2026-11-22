"""Offline model doubles for agent and team tests."""
from __future__ import annotations

import copy
import itertools
import json
import re
import threading
from typing import Any, Callable

from miniagent.config import Limits, Settings, TeamLimits
from miniagent.context import estimate_tokens
from miniagent.providers import Provider
from miniagent.schemas import LLMResponse, ToolCall, Usage

_ids = itertools.count(1)


def call(name: str, /, **arguments: Any) -> dict:
    return {"tool": name, "arguments": arguments}


def reply(text: str = "", *calls: dict, finish: str | None = None) -> LLMResponse:
    tool_calls = [ToolCall(id=f"call_{next(_ids)}", name=c["tool"], arguments=c["arguments"],
                           raw_arguments=json.dumps(c["arguments"], ensure_ascii=False)) for c in calls]
    return LLMResponse(text=text, tool_calls=tool_calls, usage=Usage(prompt_tokens=100, completion_tokens=10),
                       finish_reason=finish or ("tool_calls" if tool_calls else "stop"), model="fake")


def agent_of(messages: list[dict]) -> str:
    match = re.search(r"\(id `([^`]+)`\)", messages[0].get("content", ""))
    return match.group(1) if match else "?"


def tool_results(messages: list[dict]) -> list[str]:
    return [m["content"] for m in messages if m.get("role") == "tool"]


class FakeProvider(Provider):
    """Calls ``respond(agent, messages, tools, options)`` for each request; thread safe."""

    name = "fake"
    transport = "offline"

    def __init__(self, respond: Callable[[str, list[dict], list | None, dict], LLMResponse]) -> None:
        self.respond = respond
        self.requests: list[dict] = []
        self.lock = threading.Lock()

    def chat(self, messages, tools=None, temperature=0.0, **options):
        snapshot = copy.deepcopy(messages)
        agent = "room" if any(t.name == "start_room_task" for t in tools or []) else agent_of(snapshot)
        with self.lock:
            self.requests.append({"agent": agent, "messages": snapshot,
                                  "tools": [t.name for t in tools or []], "options": options})
        response = self.respond(agent, snapshot, tools, options)
        # Report a realistic prompt size, like a real provider would.
        return response.model_copy(update={"usage": Usage(prompt_tokens=estimate_tokens(snapshot),
                                                          completion_tokens=response.usage.completion_tokens)})

    def for_agent(self, agent: str) -> list[dict]:
        return [r for r in self.requests if r["agent"] == agent]


class Script:
    """Per-agent queues of responses; ``default`` answers when a queue is empty."""

    def __init__(self, default: Callable[[str, list[dict]], LLMResponse] | None = None, **queues) -> None:
        self.queues = {agent: list(items) for agent, items in queues.items()}
        self.default = default

    def __call__(self, agent, messages, tools, options):
        queue = self.queues.get(agent)
        if queue:
            item = queue.pop(0)
            return item(messages) if callable(item) else item
        if self.default:
            return self.default(agent, messages)
        raise AssertionError(f"no scripted response left for {agent}")


def settings(tmp, **changes) -> Settings:
    base = dict(runs_dir=tmp, api_key="", limits=Limits(max_steps=12), team=TeamLimits(lead_max_steps=12))
    base.update(changes)
    return Settings(**base)
