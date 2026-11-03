"""Agent 循环测试：正常收尾、步数护栏、模型出错、参数非法。"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from miniagent.agent import Agent
from miniagent.config import Limits, Settings
from miniagent.providers import Provider, ProviderError, ScriptedProvider
from miniagent.sandbox import Sandbox
from miniagent.schemas import LLMResponse, ToolCall
from miniagent.tools import build_default_registry
from tests.support import cleanup, make_temp_dir


def _settings(**limit_kwargs: Any) -> Settings:
    settings = Settings()
    settings.limits = Limits(**limit_kwargs)
    return settings


class AgentLoopTest(unittest.TestCase):
    def setUp(self) -> None:
        self.run_dir = make_temp_dir("agent")

    def tearDown(self) -> None:
        cleanup(self.run_dir)

    def _agent(self, provider: Provider, settings: Settings | None = None, name: str = "worker") -> Agent:
        return Agent(
            name=name,
            provider=provider,
            registry=build_default_registry(),
            sandbox=Sandbox(self.run_dir / "agents" / name, agent_name=name),
            settings=settings or _settings(),
        )

    # ------------------------------------------------------------------
    def test_completes_and_writes_file(self) -> None:
        provider = ScriptedProvider(
            [
                {"tool_calls": [{"name": "write_file", "arguments": {"path": "a.md", "content": "hello"}}]},
                {"text": "done"},
            ]
        )
        result = self._agent(provider).run("写一个文件")
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(result.steps, 2)
        self.assertEqual(result.tool_calls, 1)
        self.assertTrue((Path(result.sandbox) / "a.md").exists())
        self.assertEqual(result.answer, "done")

    def test_stops_at_max_steps(self) -> None:
        provider = ScriptedProvider(
            [
                {"tool_calls": [{"name": "list_dir", "arguments": {"path": "."}}]},
                {"tool_calls": [{"name": "list_dir", "arguments": {"path": "."}}]},
                {"tool_calls": [{"name": "list_dir", "arguments": {"path": "."}}]},
            ]
        )
        result = self._agent(provider, _settings(max_steps=2)).run("永远做事")
        self.assertEqual(result.status, "limit_exceeded")
        self.assertIn("最大步数", result.error)
        self.assertEqual(result.steps, 2)

    def test_provider_error_is_caught(self) -> None:
        class _Boom(Provider):
            name = "boom"

            def chat(self, messages, tools=None, temperature=0.0):  # type: ignore[override]
                raise ProviderError("模拟限流")

        result = self._agent(_Boom()).run("任意任务")
        self.assertEqual(result.status, "error")
        self.assertIn("模拟限流", result.error)

    def test_malformed_tool_arguments_are_fed_back(self) -> None:
        class _BadArgs(Provider):
            name = "bad"

            def __init__(self) -> None:
                self.calls = 0

            def chat(self, messages, tools=None, temperature=0.0):  # type: ignore[override]
                self.calls += 1
                if self.calls == 1:
                    return LLMResponse(
                        tool_calls=[
                            ToolCall(
                                id="c1",
                                name="write_file",
                                arguments={},
                                raw_arguments="{not json",
                                parse_error="参数不是合法 JSON",
                            )
                        ]
                    )
                return LLMResponse(text="已改用正确参数")

        result = self._agent(_BadArgs()).run("坏参数测试")
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(result.tool_calls, 0)  # 参数非法，没有真正执行工具

    def test_unknown_tool_does_not_crash(self) -> None:
        provider = ScriptedProvider(
            [
                {"tool_calls": [{"name": "nonexistent_tool", "arguments": {}}]},
                {"text": "换了个办法"},
            ]
        )
        result = self._agent(provider).run("调一个不存在的工具")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.steps, 2)

    def test_time_budget_guard(self) -> None:
        provider = ScriptedProvider(
            [{"tool_calls": [{"name": "list_dir", "arguments": {"path": "."}}]} for _ in range(5)]
        )
        result = self._agent(provider, _settings(max_seconds=-1)).run("立刻超时")
        self.assertEqual(result.status, "limit_exceeded")
        self.assertIn("时间上限", result.error)


if __name__ == "__main__":
    unittest.main()
