"""编排器测试：计划校验、S1 串行拓扑、S2 分层并发、失败局部传播。

用一个"按任务名分派剧本"的假 provider：既能模拟规划器的 JSON 输出，
也能为每个子任务提供不同的决策序列，并且能测出真实的并发（记录同时进行的调用数）。
"""

from __future__ import annotations

import json
import threading
import time
import unittest
from pathlib import Path

from miniagent.config import Limits, Settings
from miniagent.metrics import load_events
from miniagent.orchestrator import (
    AgentCard,
    Board,
    Orchestrator,
    PlanError,
    extract_json,
    route,
    topological_layers,
    topological_order,
    validate_plan,
)
from miniagent.providers import Provider
from miniagent.schemas import LLMResponse, ToolCall, Usage
from tests.support import cleanup, make_temp_dir

ROSTER = [
    AgentCard(name="worker", skills=["通用", "整理"]),
    AgentCard(name="researcher", skills=["调研", "检索", "资料"]),
    AgentCard(name="writer", skills=["写作", "报告"]),
]


def _scheduler_acceptance(_workspace: Path) -> tuple[bool, str]:
    """Trusted fixture for scheduling-only tests; artifact checks live separately."""
    return True, "scheduler fixture accepted by the test harness"


def _text(text: str) -> LLMResponse:
    return LLMResponse(
        text=text, finish_reason="stop", usage=Usage(prompt_tokens=20, completion_tokens=10)
    )


class TaskAwareProvider(Provider):
    """按用户消息（子任务 objective）分派剧本；线程安全，可测并发。"""

    name = "task-aware"

    def __init__(
        self,
        plan: dict,
        responses: dict[str, list[dict]],
        delay: float = 0.0,
    ) -> None:
        self.plan = plan
        self.responses = {key: list(value) for key, value in responses.items()}
        self.delay = delay
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0
        self.planner_calls = 0

    def chat(self, messages, tools=None, temperature=0.0):  # type: ignore[override]
        system = str(messages[0].get("content") or "")
        if "任务规划器" in system:
            with self._lock:
                self.planner_calls += 1
            return _text(json.dumps(self.plan, ensure_ascii=False))

        user = next(
            (str(m.get("content") or "") for m in messages if m.get("role") == "user"), ""
        )
        key = next((k for k in self.responses if k in user), "")
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            if self.delay:
                time.sleep(self.delay)
            with self._lock:
                queue = self.responses.get(key) or [{"text": "默认完成"}]
                item = queue.pop(0) if len(queue) > 1 else queue[0]
            return self._build(item)
        finally:
            with self._lock:
                self._active -= 1

    def _build(self, item: dict) -> LLMResponse:
        if item.get("tool_calls"):
            calls = [
                ToolCall(
                    id=f"c{index}",
                    name=str(call["name"]),
                    arguments=dict(call.get("arguments") or {}),
                    raw_arguments=json.dumps(call.get("arguments") or {}, ensure_ascii=False),
                )
                for index, call in enumerate(item["tool_calls"])
            ]
            return LLMResponse(
                text="",
                tool_calls=calls,
                finish_reason="tool_calls",
                usage=Usage(prompt_tokens=20, completion_tokens=10),
            )
        return _text(str(item.get("text") or ""))


def _plan(*subtasks: dict) -> dict:
    return {"subtasks": list(subtasks)}


def _subtask(task_id: str, objective: str, deps: list[str] | None = None, **extra) -> dict:
    payload = {
        "id": task_id,
        "objective": objective,
        "assignee": extra.pop("assignee", "worker"),
        "deps": deps or [],
        "acceptance": extra.pop("acceptance", "产出非空文件"),
    }
    payload.update(extra)
    return payload


class OrchestratorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.base = make_temp_dir("orch")
        self.run_dir = self.base / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        cleanup(self.base)

    def _orchestrator(self, provider: Provider, **kwargs) -> Orchestrator:
        settings = Settings()
        settings.limits = kwargs.pop("limits", Limits())
        kwargs.setdefault("default_acceptance", _scheduler_acceptance)
        kwargs.setdefault("root_acceptance", _scheduler_acceptance)
        return Orchestrator(
            provider=provider,
            settings=settings,
            roster=kwargs.pop("roster", ROSTER),
            run_dir=self.run_dir,
            **kwargs,
        )

    # ------------------------------------------------------------ 计划校验
    def test_extract_json_tolerates_fences(self) -> None:
        payload = extract_json('说明文字\n```json\n{"subtasks": [{"id": "s1"}]}\n```\n尾巴')
        self.assertEqual(payload["subtasks"][0]["id"], "s1")

    def test_validate_plan_rejects_cycle(self) -> None:
        payload = _plan(
            _subtask("s1", "甲", deps=["s2"]),
            _subtask("s2", "乙", deps=["s1"]),
        )
        with self.assertRaises(PlanError) as ctx:
            validate_plan(payload, ROSTER, 6, "worker")
        self.assertIn("成环", str(ctx.exception))

    def test_validate_plan_rejects_unknown_dep(self) -> None:
        payload = _plan(_subtask("s1", "甲", deps=["s9"]))
        with self.assertRaises(PlanError):
            validate_plan(payload, ROSTER, 6, "worker")

    def test_validate_plan_routes_unknown_assignee_by_skill(self) -> None:
        payload = _plan(_subtask("s1", "调研 NLP 进展并整理资料", assignee="不存在的角色"))
        tasks = validate_plan(payload, ROSTER, 6, "worker")
        self.assertEqual(tasks[0].assignee, "researcher")

    def test_route_falls_back_to_default(self) -> None:
        self.assertEqual(route("ghost", "完全无关的目标", ROSTER, "worker"), "worker")

    def test_topological_layers_diamond(self) -> None:
        tasks = validate_plan(
            _plan(
                _subtask("s1", "甲"),
                _subtask("s2", "乙", deps=["s1"]),
                _subtask("s3", "丙", deps=["s1"]),
                _subtask("s4", "丁", deps=["s2", "s3"]),
            ),
            ROSTER,
            6,
            "worker",
        )
        layers = [[task.id for task in layer] for layer in topological_layers(tasks)]
        self.assertEqual(layers, [["s1"], ["s2", "s3"], ["s4"]])
        self.assertEqual([task.id for task in topological_order(tasks)], ["s1", "s2", "s3", "s4"])

    def test_plan_degrades_to_single_task(self) -> None:
        provider = TaskAwareProvider(_plan(), {})  # 空计划 → 永远不合规
        orchestrator = self._orchestrator(provider)
        try:
            tasks = orchestrator.plan("做一件说不清的大任务")
        finally:
            orchestrator.close()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].id, "s1")
        self.assertEqual(provider.planner_calls, 2)  # 重试一次后降级

    # ------------------------------------------------------------ S1 串行
    def test_serial_follows_topology(self) -> None:
        provider = TaskAwareProvider(
            _plan(
                _subtask("s1", "先写 a.md"),
                _subtask("s2", "再写 b.md", deps=["s1"]),
            ),
            {
                "先写 a.md": [{"text": "a 完成"}],
                "再写 b.md": [{"text": "b 完成"}],
            },
        )
        result = self._orchestrator(provider).run("两件有依赖的事", scheduler="serial")

        self.assertEqual(result.status, "completed")
        events = load_events(self.run_dir)
        order = [e["subtask"] for e in events if e["kind"] == "subtask_end"]
        self.assertEqual(order, ["s1", "s2"])
        self.assertEqual(result.completed, 2)

    def test_failure_blocks_only_downstream(self) -> None:
        provider = TaskAwareProvider(
            _plan(
                _subtask("s1", "注定失败的任务"),
                _subtask("s2", "依赖失败任务", deps=["s1"]),
                _subtask("s3", "无关任务"),
            ),
            {
                "注定失败的任务": [{"text": "我写完了"}],
                "依赖失败任务": [{"text": "不该跑到这里"}],
                "无关任务": [{"text": "s3 完成"}],
            },
        )
        orchestrator = self._orchestrator(
            provider,
            acceptances={"s1": lambda _ws: (False, "缺少产物")},
        )
        result = orchestrator.run("一个会部分失败的图", scheduler="serial")

        statuses = {task.id: task.status for task in result.subtasks}
        self.assertEqual(statuses["s1"], "failed")
        self.assertEqual(statuses["s2"], "blocked")
        self.assertEqual(statuses["s3"], "done")
        self.assertEqual(result.status, "partial")

    def test_plan_and_board_are_saved(self) -> None:
        provider = TaskAwareProvider(
            _plan(_subtask("s1", "写 c.md")),
            {"写 c.md": [{"text": "完成"}]},
        )
        self._orchestrator(provider).run("保存计划", scheduler="serial")
        self.assertTrue((self.run_dir / "plan.json").exists())
        self.assertTrue((self.run_dir / "board.json").exists())
        board = json.loads((self.run_dir / "board.json").read_text(encoding="utf-8"))
        self.assertEqual(board["subtasks"][0]["status"], "done")

    def test_each_subtask_receives_budget(self) -> None:
        provider = TaskAwareProvider(
            _plan(_subtask("s1", "写 d.md"), _subtask("s2", "写 e.md", deps=["s1"])),
            {"写 d.md": [{"text": "d 完成"}], "写 e.md": [{"text": "e 完成"}]},
        )
        orch = self._orchestrator(provider, limits=Limits(max_total_tokens=0))
        result = orch.run("预算切分", scheduler="serial")
        self.assertIsNone(orch._global_budget.tokens_left)
        self.assertEqual(orch._global_budget.tokens_used, result.tokens)
        self.assertGreater(orch._global_budget.tokens_used, sum(task.tokens for task in result.subtasks))
        for task in result.subtasks:
            self.assertGreaterEqual(task.steps, 1)
            self.assertGreater(task.tokens, 0)
        self.assertGreater(result.tokens, 0)

    # ------------------------------------------------------------ S2 并发
    def test_parallel_overlaps_independent_tasks(self) -> None:
        provider = TaskAwareProvider(
            _plan(_subtask("s1", "独立任务甲"), _subtask("s2", "独立任务乙")),
            {"独立任务甲": [{"text": "甲完成"}], "独立任务乙": [{"text": "乙完成"}]},
            delay=0.12,
        )
        result = self._orchestrator(provider).run(
            "两个可并行的任务", scheduler="parallel", max_parallel=2
        )
        self.assertEqual(result.status, "completed")
        self.assertGreaterEqual(provider.max_active, 2, "两个子任务应当同时在跑")

    def test_parallel_respects_max_parallel(self) -> None:
        provider = TaskAwareProvider(
            _plan(
                _subtask("s1", "并行甲"),
                _subtask("s2", "并行乙"),
                _subtask("s3", "并行丙"),
            ),
            {
                "并行甲": [{"text": "甲完成"}],
                "并行乙": [{"text": "乙完成"}],
                "并行丙": [{"text": "丙完成"}],
            },
            delay=0.08,
        )
        result = self._orchestrator(provider).run(
            "三个任务但只允许两个并发", scheduler="parallel", max_parallel=2
        )
        self.assertEqual(result.status, "completed")
        self.assertLessEqual(provider.max_active, 2)

    def test_parallel_respects_dependencies_between_layers(self) -> None:
        provider = TaskAwareProvider(
            _plan(
                _subtask("s1", "第一层任务"),
                _subtask("s2", "第二层任务", deps=["s1"]),
            ),
            {"第一层任务": [{"text": "层1完成"}], "第二层任务": [{"text": "层2完成"}]},
            delay=0.05,
        )
        result = self._orchestrator(provider).run("分层依赖", scheduler="parallel")
        self.assertEqual(result.status, "completed")
        events = load_events(self.run_dir)
        order = [e["subtask"] for e in events if e["kind"] == "subtask_end"]
        self.assertEqual(order, ["s1", "s2"])


class _RecordingOrchestrator(Orchestrator):
    """覆盖 _execute，用可控延时记录调度时序 —— 避开真实模型与工具抖动的干扰。"""

    def __init__(self, *args, delays: dict[str, float] | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.log: list[tuple[str, str, float]] = []
        self._loglock = threading.Lock()
        self.delays = delays or {}

    def _execute(self, task, board, depth: int = 0) -> None:  # type: ignore[override]
        with self._loglock:
            self.log.append(("start", task.id, time.perf_counter()))
        time.sleep(self.delays.get(task.id, 0.0))
        with self._loglock:
            self.log.append(("end", task.id, time.perf_counter()))
        task.status = "done"
        task.result = f"{task.id} 完成"


class EventedSchedulingTest(unittest.TestCase):
    """S3 与 S2 的核心差别：完成即解锁，还是等整层。"""

    def setUp(self) -> None:
        self.base = make_temp_dir("evented")
        self.run_dir = self.base / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        cleanup(self.base)

    def _orchestrator(self, delays: dict[str, float]) -> _RecordingOrchestrator:
        settings = Settings()
        settings.limits = Limits()
        return _RecordingOrchestrator(
            provider=TaskAwareProvider(_plan(), {}),
            settings=settings,
            roster=ROSTER,
            run_dir=self.run_dir,
            delays=delays,
        )

    def _tasks(self):
        return validate_plan(
            _plan(
                _subtask("s1", "甲"),
                _subtask("s2", "乙", deps=["s1"]),
                _subtask("s3", "丙"),
            ),
            ROSTER,
            6,
            "worker",
        )

    def test_evented_unlocks_right_after_dependency(self) -> None:
        orchestrator = self._orchestrator({"s1": 0.15, "s2": 0.05, "s3": 0.45})
        self.addCleanup(orchestrator.close)
        tasks = self._tasks()
        orchestrator._dispatch_evented(tasks, Board(self.run_dir), 2)

        starts = {tid: ts for kind, tid, ts in orchestrator.log if kind == "start"}
        ends = {tid: ts for kind, tid, ts in orchestrator.log if kind == "end"}
        self.assertLess(starts["s2"], ends["s3"], "依赖驱动：s2 应在 s3 结束前就开始")
        self.assertTrue(all(task.status == "done" for task in tasks))

    def test_serial_waits_for_the_whole_layer(self) -> None:
        orchestrator = self._orchestrator({"s1": 0.15, "s2": 0.05, "s3": 0.45})
        self.addCleanup(orchestrator.close)
        tasks = self._tasks()
        orchestrator._dispatch_serial(tasks, Board(self.run_dir))

        starts = {tid: ts for kind, tid, ts in orchestrator.log if kind == "start"}
        ends = {tid: ts for kind, tid, ts in orchestrator.log if kind == "end"}
        self.assertGreater(starts["s2"], ends["s3"], "串行：s2 必须等 s3 跑完")


class EventedRunTest(unittest.TestCase):
    def setUp(self) -> None:
        self.base = make_temp_dir("evented-run")
        self.run_dir = self.base / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        cleanup(self.base)

    def _orchestrator(self, provider: Provider, **kwargs) -> Orchestrator:
        settings = Settings()
        settings.limits = kwargs.pop("limits", Limits())
        kwargs.setdefault("default_acceptance", _scheduler_acceptance)
        kwargs.setdefault("root_acceptance", _scheduler_acceptance)
        return Orchestrator(
            provider=provider, settings=settings, roster=ROSTER, run_dir=self.run_dir, **kwargs
        )

    def test_evented_completes_independent_tasks(self) -> None:
        provider = TaskAwareProvider(
            _plan(_subtask("s1", "独立甲"), _subtask("s2", "独立乙")),
            {"独立甲": [{"text": "甲完成"}], "独立乙": [{"text": "乙完成"}]},
        )
        result = self._orchestrator(provider).run("事件驱动跑两个", scheduler="evented")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.completed, 2)

    def test_evented_failure_blocks_downstream_and_terminates(self) -> None:
        provider = TaskAwareProvider(
            _plan(
                _subtask("s1", "会失败的任务"),
                _subtask("s2", "依赖它", deps=["s1"]),
                _subtask("s3", "无关任务"),
            ),
            {
                "会失败的任务": [{"text": "假完成"}],
                "依赖它": [{"text": "不该跑"}],
                "无关任务": [{"text": "完成"}],
            },
        )
        orchestrator = self._orchestrator(
            provider, acceptances={"s1": lambda _ws: (False, "缺产物")}
        )
        result = orchestrator.run("部分失败的图", scheduler="evented")
        statuses = {task.id: task.status for task in result.subtasks}
        self.assertEqual(statuses["s1"], "failed")
        self.assertEqual(statuses["s2"], "blocked")
        self.assertEqual(statuses["s3"], "done")
        self.assertEqual(result.status, "partial")

    def test_evented_respects_max_parallel(self) -> None:
        provider = TaskAwareProvider(
            _plan(
                _subtask("s1", "并行甲"),
                _subtask("s2", "并行乙"),
                _subtask("s3", "并行丙"),
            ),
            {
                "并行甲": [{"text": "甲完成"}],
                "并行乙": [{"text": "乙完成"}],
                "并行丙": [{"text": "丙完成"}],
            },
            delay=0.08,
        )
        result = self._orchestrator(provider).run(
            "三个任务两个并发", scheduler="evented", max_parallel=2
        )
        self.assertEqual(result.status, "completed")
        self.assertLessEqual(provider.max_active, 2)


class DelegationTest(unittest.TestCase):
    """委派：agent 用 delegate_task 把子任务真的派出去执行（课程 2.1b）。"""

    def setUp(self) -> None:
        self.base = make_temp_dir("delegate")
        self.run_dir = self.base / "run"
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        cleanup(self.base)

    def _provider(self) -> TaskAwareProvider:
        return TaskAwareProvider(
            _plan(_subtask("s1", "母任务")),
            {
                "母任务": [
                    {"tool_calls": [{"name": "delegate_task", "arguments": {"objective": "子任务X"}}]},
                    {"text": "母任务完成"},
                ],
                "子任务X": [{"text": "子任务完成"}],
            },
        )

    def _run(self, **kwargs) -> tuple[object, list[str]]:
        settings = Settings()
        kwargs.setdefault("default_acceptance", _scheduler_acceptance)
        kwargs.setdefault("root_acceptance", _scheduler_acceptance)
        orchestrator = Orchestrator(
            provider=self._provider(),
            settings=settings,
            roster=ROSTER,
            run_dir=self.run_dir,
            **kwargs,
        )
        result = orchestrator.run("委派演示", scheduler="serial")
        bus_lines = (self.run_dir / "bus.jsonl").read_text(encoding="utf-8").splitlines()
        return result, bus_lines

    def test_delegation_runs_child_task(self) -> None:
        result, bus_lines = self._run(enable_delegation=True)
        self.assertEqual(result.status, "completed")
        self.assertTrue(
            any("delegate_request" in line and "s1.d1" in line for line in bus_lines)
        )

    def test_delegation_disabled_degrades(self) -> None:
        result, bus_lines = self._run(enable_delegation=False)
        self.assertEqual(result.status, "completed")
        self.assertFalse(any("delegate_request" in line for line in bus_lines))

    def test_delegation_depth_limit(self) -> None:
        result, bus_lines = self._run(enable_delegation=True, max_delegation_depth=0)
        self.assertEqual(result.status, "completed")
        self.assertFalse(any("delegate_request" in line for line in bus_lines))


if __name__ == "__main__":
    unittest.main()
