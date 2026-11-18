"""RAO 原子单元测试：这是"单步可断言"的证据，也是本设计的核心卖点。

三层：
1. 纯单步：给定 mock 决策 → 断言观察、预算、终止信号（不跑循环）
2. 循环级：Agent 驱动多步 → 断言状态映射（aborted / failed_acceptance）
3. 契约级：决策闭集、错误分类、验收函数
"""

from __future__ import annotations

import unittest
from pathlib import Path

from miniagent.agent import Agent
from miniagent.config import Limits, Settings
from miniagent.metrics import Tracer, load_events
from miniagent.providers import ScriptedProvider
from miniagent.rao import (
    Budget,
    ErrorClass,
    RAOState,
    RAOUnit,
    accept_files,
    classify_failure,
    evaluate_acceptance,
)
from miniagent.sandbox import Sandbox
from miniagent.schemas import LLMResponse, ToolCall
from miniagent.tools import build_default_registry
from tests.support import cleanup, make_temp_dir


def _limits(**kwargs) -> Limits:
    return Limits(**kwargs)


def _unit(root: Path, script: list[dict], **kwargs) -> RAOUnit:
    return RAOUnit(
        name="worker",
        provider=ScriptedProvider(script),
        registry=build_default_registry(),
        sandbox=Sandbox(root),
        limits=kwargs.pop("limits", _limits()),
        **kwargs,
    )


def _state() -> RAOState:
    return RAOState(
        goal="测试目标",
        task_id="t1",
        history=[{"role": "user", "content": "测试目标"}],
    )


def _agent(root: Path, script: list[dict], **kwargs) -> Agent:
    settings = Settings()
    settings.limits = kwargs.pop("limits", _limits())
    return Agent(
        name="worker",
        provider=ScriptedProvider(script),
        registry=build_default_registry(),
        sandbox=Sandbox(root),
        settings=settings,
        **kwargs,
    )


class RaoStepTest(unittest.TestCase):
    """第 1 层：单步断言。"""

    def setUp(self) -> None:
        self.base = make_temp_dir("rao")
        self.root = self.base / "agent"

    def tearDown(self) -> None:
        cleanup(self.base)

    def test_act_observation_carries_artifacts(self) -> None:
        unit = _unit(self.root, [])
        state = _state()
        unit.provider = ScriptedProvider(
            [
                {
                    "tool_calls": [
                        {
                            "name": "write_file",
                            "arguments": {"path": "a.md", "content": "hello world"},
                        }
                    ]
                }
            ]
        )
        record = unit.step(state, Budget())

        self.assertEqual(record.decision_kind, "act")
        self.assertEqual(len(record.observations), 1)
        obs = record.observations[0]
        self.assertTrue(obs.ok)
        self.assertEqual(obs.artifacts, ["a.md"])
        self.assertEqual(record.tool_calls, 1)
        self.assertIsNone(record.error_class)
        self.assertTrue((self.root / "a.md").exists())

    def test_observe_classifies_sandbox_denial(self) -> None:
        unit = _unit(
            self.root,
            [
                {
                    "tool_calls": [
                        {"name": "write_file", "arguments": {"path": "../evil.md", "content": "x"}}
                    ]
                }
            ],
        )
        record = unit.step(_state(), Budget())
        self.assertFalse(record.observations[0].ok)
        self.assertEqual(record.observations[0].error_class, ErrorClass.A_SANDBOX_DENY)
        self.assertEqual(record.tool_calls, 0)

    def test_parse_response_maps_closed_set(self) -> None:
        unit = _unit(self.root, [])
        act = unit.parse_response(
            LLMResponse(tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "a"})])
        )
        self.assertEqual(act.kind, "act")
        self.assertEqual(act.actions[0].tool, "read_file")

        answer = unit.parse_response(LLMResponse(text="做完了"))
        self.assertEqual(answer.kind, "answer")
        self.assertEqual(answer.answer, "做完了")

        empty = unit.parse_response(LLMResponse(text="   "))
        self.assertEqual(empty.kind, "act")
        self.assertEqual(empty.actions, [])

    def test_empty_decision_is_invalid(self) -> None:
        unit = _unit(self.root, [{"text": "  "}])
        record = unit.step(_state(), Budget())
        self.assertEqual(record.error_class, ErrorClass.R_INVALID_DECISION)
        self.assertFalse(record.ok)

    def test_loop_warning_after_three_repeats(self) -> None:
        same = {"tool_calls": [{"name": "list_dir", "arguments": {"path": "."}}]}
        unit = _unit(self.root, [same, same, same])
        state = _state()
        budget = Budget()
        for _ in range(3):
            record = unit.step(state, budget)
        self.assertIsNone(record.terminate_reason)
        self.assertTrue(
            any("已连续重复" in str(m.get("content", "")) for m in state.history)
        )

    def test_loop_terminates_after_five_repeats(self) -> None:
        same = {"tool_calls": [{"name": "list_dir", "arguments": {"path": "."}}]}
        unit = _unit(self.root, [same] * 6)
        state = _state()
        budget = Budget()
        record = None
        for _ in range(6):
            record = unit.step(state, budget)
            if record.terminate_reason:
                break
        assert record is not None
        self.assertEqual(record.terminate_reason, ErrorClass.T_LOOP)

    def test_ask_human_without_handler_stops(self) -> None:
        unit = _unit(
            self.root,
            [{"tool_calls": [{"name": "ask_human", "arguments": {"question": "选 A 还是 B？"}}]}],
        )
        record = unit.step(_state(), Budget())
        self.assertEqual(record.decision_kind, "ask_human")
        self.assertEqual(record.observations[0].error_class, ErrorClass.T_HUMAN_UNAVAILABLE)
        self.assertEqual(record.terminate_reason, ErrorClass.T_HUMAN_UNAVAILABLE)

    def test_ask_human_with_handler_returns_reply(self) -> None:
        unit = _unit(
            self.root,
            [{"tool_calls": [{"name": "ask_human", "arguments": {"question": "选 A 还是 B？"}}]}],
            human_handler=lambda question: "选 A",
        )
        record = unit.step(_state(), Budget())
        self.assertTrue(record.observations[0].ok)
        self.assertEqual(record.observations[0].content, "选 A")
        self.assertEqual(record.observations[0].source, "human")

    def test_delegate_without_orchestrator_is_unsupported(self) -> None:
        unit = _unit(
            self.root,
            [{"tool_calls": [{"name": "delegate_task", "arguments": {"objective": "查资料"}}]}],
        )
        record = unit.step(_state(), Budget())
        self.assertEqual(record.decision_kind, "delegate")
        self.assertEqual(record.observations[0].error_class, ErrorClass.A_UNSUPPORTED)

    def test_delegate_with_handler_returns_observation(self) -> None:
        from miniagent.rao import Observation

        def handler(payload: dict) -> Observation:
            return Observation(
                step_id="x", source="delegate", ok=True, content=f"已交给 {payload['objective']}"
            )

        unit = _unit(
            self.root,
            [{"tool_calls": [{"name": "delegate_task", "arguments": {"objective": "查资料"}}]}],
            delegate_handler=handler,
        )
        record = unit.step(_state(), Budget())
        self.assertTrue(record.observations[0].ok)
        self.assertIn("查资料", record.observations[0].content)


class BudgetTest(unittest.TestCase):
    def test_spend_and_exhaust(self) -> None:
        budget = Budget(steps_left=2, seconds_left=10.0, tokens_left=100, actions_left=4)
        self.assertFalse(budget.exhausted)
        budget.spend(steps=2)
        self.assertTrue(budget.exhausted)
        self.assertIn("最大步数", budget.reason(Limits(max_steps=2)))

    def test_seconds_reason_takes_priority(self) -> None:
        budget = Budget(steps_left=0, seconds_left=-1.0, tokens_left=100, actions_left=4)
        self.assertIn("时间上限", budget.reason(Limits(max_seconds=30)))

    def test_fork_splits_remaining(self) -> None:
        budget = Budget(steps_left=10, seconds_left=100.0, tokens_left=1000, actions_left=10)
        child = budget.fork(0.5)
        self.assertEqual(child.steps_left, 5)
        self.assertEqual(budget.steps_left, 5)
        self.assertEqual(child.tokens_left + budget.tokens_left, 1000)


class ContractTest(unittest.TestCase):
    """第 3 层：错误分类与验收契约。"""

    def test_classify_failure_table(self) -> None:
        cases = {
            "沙箱拒绝访问：路径越界": ErrorClass.A_SANDBOX_DENY,
            "已取消：删除文件需要人类批准（HITL 拦截）": ErrorClass.A_HUMAN_DENIED,
            "参数校验失败：缺少必填参数 path": ErrorClass.A_INVALID_ARGS,
            "未知工具 'rm_rf'。可用工具：...": ErrorClass.A_UNKNOWN_TOOL,
            "工具 run_python 执行超时。": ErrorClass.A_TOOL_TIMEOUT,
            "工具 run_python 执行异常：ValueError: boom": ErrorClass.A_TOOL_ERROR,
        }
        for content, expected in cases.items():
            with self.subTest(content=content):
                self.assertEqual(classify_failure(content), expected)

    def test_accept_files(self) -> None:
        base = make_temp_dir("accept")
        try:
            check = accept_files("a.md")
            ok, reason = check(base)
            self.assertFalse(ok)
            self.assertIn("缺少产物", reason)

            (base / "a.md").write_text("短", encoding="utf-8")
            ok, reason = check(base)
            self.assertFalse(ok)

            (base / "a.md").write_text("# 标题\n\n内容足够长的一段文字。\n", encoding="utf-8")
            ok, reason = check(base)
            self.assertTrue(ok, reason)
        finally:
            cleanup(base)

    def test_acceptance_none_is_not_verified(self) -> None:
        ok, reason = evaluate_acceptance(None, Path("."))
        self.assertFalse(ok)
        self.assertEqual(reason, "no-acceptance")

    def test_acceptance_exception_fails_safe(self) -> None:
        def broken(_workspace: Path) -> tuple[bool, str]:
            raise RuntimeError("checker bug")

        ok, reason = evaluate_acceptance(broken, Path("."))
        self.assertFalse(ok)
        self.assertIn("验收函数异常", reason)


class AgentTerminationTest(unittest.TestCase):
    """第 2 层：终态映射。"""

    def setUp(self) -> None:
        self.base = make_temp_dir("rao-agent")
        self.root = self.base / "agent"

    def tearDown(self) -> None:
        cleanup(self.base)

    def test_abort_tool_maps_to_aborted(self) -> None:
        agent = _agent(
            self.root,
            [{"tool_calls": [{"name": "abort_task", "arguments": {"reason": "缺少必要数据"}}]}],
        )
        result = agent.run("做不了的任务")
        self.assertEqual(result.status, "aborted")
        self.assertIn("中止", result.error)

    def test_acceptance_failure_then_success(self) -> None:
        attempts = {"n": 0}

        def checker(_workspace: Path) -> tuple[bool, str]:
            attempts["n"] += 1
            return (attempts["n"] >= 2, "还差一点")

        agent = _agent(self.root, [{"text": "第一次"}, {"text": "第二次"}])
        result = agent.run("带验收的任务", acceptance=checker)
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(result.steps, 2)

    def test_acceptance_failure_twice_is_failed_acceptance(self) -> None:
        agent = _agent(self.root, [{"text": "敷衍"}, {"text": "还是敷衍"}])
        result = agent.run("验收不过的任务", acceptance=lambda _ws: (False, "缺少 notes.md"))
        self.assertEqual(result.status, "failed_acceptance")
        self.assertIn(ErrorClass.T_ACCEPTANCE_FAILED, result.error)

    def test_trace_records_acceptance_event(self) -> None:
        run_dir = self.base / "run"
        with Tracer(run_dir) as tracer:
            agent = _agent(self.root, [{"text": "完成"}], tracer=tracer)
            result = agent.run("记录验收", acceptance=lambda _ws: (True, "ok"))
        self.assertEqual(result.status, "completed")
        kinds = [event["kind"] for event in load_events(run_dir)]
        self.assertIn("acceptance", kinds)
        self.assertIn("step", kinds)
        self.assertIn("task_end", kinds)


if __name__ == "__main__":
    unittest.main()
