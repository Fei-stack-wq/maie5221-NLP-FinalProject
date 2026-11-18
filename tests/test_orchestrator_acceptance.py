"""Goal acceptance must inspect this attempt's products, independently of children."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from urllib.parse import quote

from miniagent.acceptance import accept_files
from miniagent.config import Settings
from miniagent.metrics import load_events
from miniagent.orchestrator import Orchestrator
from tests.support import cleanup, make_temp_dir
from tests.test_orchestrator import ROSTER, TaskAwareProvider, _plan, _subtask


def _writes(content: str = "verified output") -> list[dict]:
    return [
        {"tool_calls": [{"name": "write_file", "arguments": {
            "path": "result.txt", "content": content,
        }}]},
        {"text": "I have completed the task."},
    ]


def _root_file(task_id: str = "s1") -> str:
    return f"agents/task-{quote(task_id, safe='')}/result.txt"


def _root_checker(task_id: str = "s1", expected: str = "verified output"):
    name = _root_file(task_id)
    return accept_files(name, min_bytes=1, contains={name: expected})


def _child_checker(expected: str = "verified output"):
    return accept_files("result.txt", min_bytes=1, contains={"result.txt": expected})


class OrchestratorAcceptanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.base = make_temp_dir("orch-acceptance")
        self.run_dir = self.base / "run"

    def tearDown(self) -> None:
        cleanup(self.base)

    def _orchestrator(self, provider=None, **kwargs) -> Orchestrator:
        if provider is None:
            provider = TaskAwareProvider(_plan(_subtask("s1", "produce result")), {
                "produce result": _writes(),
            })
        kwargs.setdefault("root_acceptance", _root_checker())
        kwargs.setdefault("default_acceptance", _child_checker())
        return Orchestrator(
            provider=provider, settings=Settings(), roster=ROSTER,
            run_dir=self.run_dir, **kwargs,
        )

    def test_file_content_reports_and_receipts_match_execution_identity(self) -> None:
        result = self._orchestrator().run("produce verified content", goal_id="goal-a")
        self.assertTrue(result.succeeded)
        self.assertEqual(result.root_id, "goal-a")
        self.assertEqual(Path(result.workspace).parent, self.run_dir / "workspaces")
        self.assertEqual((Path(result.workspace) / _root_file()).read_text(), "verified output")
        child = result.subtasks[0]
        self.assertEqual(child.task_id, "goal-a:s1")
        self.assertEqual(child.parent_id, "goal-a")
        self.assertEqual(child.acceptance_report.task_id, child.task_id)
        self.assertEqual(child.acceptance_report.attempt_id, child.attempt_id)
        self.assertEqual(result.acceptance_report.attempt_id, result.attempt_id)
        self.assertTrue(result.acceptance_evidence)
        self.assertTrue(child.acceptance_evidence)
        self.assertTrue(result.acceptance_report.evaluated_resource_versions)

        events = load_events(self.run_dir)
        child_start = next(e for e in events if e["kind"] == "task_start")
        child_end = next(e for e in events if e["kind"] == "subtask_end")
        root_end = next(e for e in events if e["kind"] == "orchestration_end")
        self.assertEqual(child_start["task_id"], child_end["task_id"])
        self.assertEqual(child_start["attempt_id"], child_end["attempt_id"])
        self.assertEqual(child_end["acceptance_report"], child.acceptance_report.model_dump(mode="json"))
        self.assertEqual(child_end["acceptance_evidence"], child.acceptance_evidence)
        self.assertEqual(root_end["acceptance_evidence"], result.acceptance_evidence)
        self.assertEqual(root_end["acceptance_report"], result.acceptance_report.model_dump(mode="json"))
        self.assertTrue(all(e["schema_version"] == 2 for e in events))
        self.assertLessEqual(result.acceptance_spec.created_at, child.acceptance_spec.created_at)

    def test_passed_children_do_not_pass_failed_root_content_check(self) -> None:
        result = self._orchestrator(root_acceptance=_root_checker(expected="different final result")).run("goal")
        self.assertEqual(result.subtasks[0].status, "done")
        self.assertEqual(result.acceptance_report.verdict, "failed")
        self.assertEqual(result.status, "failed_acceptance")
        self.assertFalse(result.succeeded)

    def test_without_trace_output_result_retains_receipts_and_workspace(self) -> None:
        provider = TaskAwareProvider(_plan(_subtask("s1", "produce result")), {"produce result": _writes()})
        orchestrator = Orchestrator(
            provider=provider, settings=Settings(), default_acceptance=_child_checker(),
            root_acceptance=_root_checker(),
        )
        result = orchestrator.run("goal")
        self.addCleanup(cleanup, Path(result.workspace))
        self.assertTrue(result.succeeded)
        self.assertIsNone(orchestrator.tracer)
        self.assertTrue(result.acceptance_evidence)
        self.assertTrue(result.subtasks[0].acceptance_evidence)
        self.assertEqual((Path(result.workspace) / _root_file()).read_text(), "verified output")

    def test_passed_children_without_root_checker_remain_unverified(self) -> None:
        result = self._orchestrator(root_acceptance=None).run("goal")
        self.assertEqual(result.subtasks[0].status, "done")
        self.assertEqual(result.status, "unverified")
        self.assertEqual(result.acceptance_report.verdict, "unverified")
        self.assertIsNone(result.acceptance_spec)
        self.assertFalse(result.succeeded)

    def test_failed_child_is_not_hidden_by_missing_root_checker(self) -> None:
        result = self._orchestrator(
            root_acceptance=None, default_acceptance=_child_checker(expected="missing content"),
        ).run("goal")
        self.assertEqual(result.subtasks[0].status, "failed")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.acceptance_report.verdict, "unverified")
        self.assertIn("s1", result.error)
        self.assertNotIn("no-acceptance", result.error)

    def test_root_checker_exception_is_error(self) -> None:
        def broken_checker(workspace):
            (workspace / _root_file()).read_text()
            raise RuntimeError("checker cannot finish")

        result = self._orchestrator(root_acceptance=broken_checker).run("goal")
        self.assertEqual(result.status, "error")
        self.assertEqual(result.acceptance_report.verdict, "error")
        self.assertIn("checker cannot finish", result.error)
        self.assertFalse(result.succeeded)

    def test_unverified_children_block_dependencies_in_every_scheduler(self) -> None:
        for scheduler in ("serial", "parallel", "evented"):
            with self.subTest(scheduler=scheduler):
                provider = TaskAwareProvider(
                    _plan(_subtask("s1", "unverified first"), _subtask("s2", "must stay blocked", deps=["s1"])),
                    {"unverified first": _writes(), "must stay blocked": _writes()},
                )
                result = self._orchestrator(provider, default_acceptance=None).run("goal", scheduler=scheduler)
                self.assertEqual([t.status for t in result.subtasks], ["unverified", "blocked"])
                self.assertEqual(result.acceptance_report.verdict, "passed")
                self.assertEqual(result.status, "failed")
                self.assertFalse(result.succeeded)
                self.assertEqual(result.subtasks[1].attempt_id, "")
                self.assertIsNone(result.subtasks[1].acceptance_report)

    def test_independent_goals_get_new_workspaces_without_old_file_success(self) -> None:
        orchestrator = self._orchestrator()
        first = orchestrator.run("goal", goal_id="first-root")
        second = orchestrator.run("goal", task_id="second-root")
        self.assertTrue(first.succeeded)
        self.assertNotEqual(first.root_id, second.root_id)
        self.assertNotEqual(first.attempt_id, second.attempt_id)
        self.assertNotEqual(first.workspace, second.workspace)
        self.assertNotEqual(first.subtasks[0].task_id, second.subtasks[0].task_id)
        self.assertNotEqual(first.subtasks[0].attempt_id, second.subtasks[0].attempt_id)
        self.assertFalse((Path(second.workspace) / _root_file()).exists())
        self.assertEqual(second.status, "failed_acceptance")
        self.assertEqual(second.acceptance_report.verdict, "failed")
        ends = [e for e in load_events(self.run_dir) if e["kind"] == "orchestration_end"]
        self.assertEqual(len(ends), 2)
        self.assertEqual({e["root_id"] for e in ends}, {"first-root", "second-root"})

    def test_missing_goal_id_generates_distinct_logical_goals(self) -> None:
        orchestrator = self._orchestrator()
        first, second = orchestrator.run("first"), orchestrator.run("second")
        self.assertNotEqual(first.root_id, second.root_id)
        self.assertNotEqual(first.subtasks[0].task_id, second.subtasks[0].task_id)

    def test_conflicting_goal_aliases_are_rejected_before_registration(self) -> None:
        orchestrator = self._orchestrator()
        self.addCleanup(orchestrator.close)
        with self.assertRaisesRegex(ValueError, "same logical goal"):
            orchestrator.run("goal", task_id="a", goal_id="b")
        self.assertEqual(load_events(self.run_dir), [])

    def test_planner_exception_still_records_registered_root_terminal(self) -> None:
        class BrokenPlanner(TaskAwareProvider):
            def chat(self, *args, **kwargs):
                raise RuntimeError("planner offline")

        result = self._orchestrator(BrokenPlanner({}, {})).run("goal", goal_id="offline-goal")
        self.assertEqual(result.status, "error")
        self.assertIn("planner offline", result.error)
        self.assertEqual(result.subtasks, [])
        events = load_events(self.run_dir)
        self.assertEqual(events[0]["kind"], "case_registered")
        self.assertEqual(events[-1]["kind"], "orchestration_end")
        self.assertEqual(events[-1]["status"], "error")
        self.assertEqual(events[-1]["attempt_id"], result.attempt_id)

    def test_execution_exception_cannot_be_hidden_by_passed_root_report(self) -> None:
        orchestrator = self._orchestrator()

        def execute_then_fail(tasks, board):
            orchestrator._execute(tasks[0], board)
            raise RuntimeError("scheduler stopped after child completion")

        orchestrator._dispatch_serial = execute_then_fail
        result = orchestrator.run("goal")
        self.assertEqual(result.acceptance_report.verdict, "passed")
        self.assertEqual(result.subtasks[0].status, "done")
        self.assertEqual(result.status, "error")
        self.assertFalse(result.succeeded)
        end = load_events(self.run_dir)[-1]
        self.assertIn("scheduler stopped", end["execution_error"])

    def test_planned_children_are_registered_even_when_dispatch_crashes(self) -> None:
        provider = TaskAwareProvider(_plan(_subtask("s1", "one"), _subtask("s2", "two")), {})
        orchestrator = self._orchestrator(provider)

        def fail_before_work(_tasks, _board):
            raise RuntimeError("dispatch offline")

        orchestrator._dispatch_serial = fail_before_work
        result = orchestrator.run("goal", goal_id="registered-goal")
        events = load_events(self.run_dir)
        registered = [e for e in events if e["kind"] == "task_registered"]
        self.assertEqual({e["task_id"] for e in registered}, {"registered-goal:s1", "registered-goal:s2"})
        self.assertEqual([t.status for t in result.subtasks], ["skipped", "skipped"])
        self.assertEqual(len([e for e in events if e["kind"] == "subtask_end"]), 2)
        self.assertFalse(any(e["kind"] == "task_start" for e in events))

    def test_special_task_and_root_ids_do_not_escape_attempt_workspace(self) -> None:
        local_id = "../../result / 子"
        provider = TaskAwareProvider(_plan(_subtask(local_id, "produce result")), {"produce result": _writes()})
        result = self._orchestrator(provider, root_acceptance=_root_checker(local_id)).run(
            "goal", goal_id="../../external-root",
        )
        self.assertTrue(result.succeeded)
        expected = Path(result.workspace) / "agents" / ("task-" + quote(local_id, safe=""))
        self.assertEqual(Path(result.subtasks[0].workspace), expected)
        self.assertTrue(expected.is_relative_to(Path(result.workspace)))
        self.assertEqual(expected.parent, Path(result.workspace) / "agents")

    def test_multiple_delegations_are_unique_registered_and_returned(self) -> None:
        provider = TaskAwareProvider(_plan(_subtask("s1", "parent work")), {
            "parent work": [
                {"tool_calls": [{"name": "delegate_task", "arguments": {"objective": "child one"}}]},
                {"tool_calls": [{"name": "delegate_task", "arguments": {"objective": "child two"}}]},
                *_writes(),
            ],
            "child one": _writes(), "child two": _writes(),
        })
        result = self._orchestrator(provider, enable_delegation=True).run("goal", goal_id="delegation")
        self.assertTrue(result.succeeded)
        self.assertEqual(result.total, 3)
        self.assertEqual(result.completed, 3)
        parent, *children = result.subtasks
        self.assertEqual(len({t.id for t in result.subtasks}), 3)
        self.assertEqual(len({t.task_id for t in result.subtasks}), 3)
        self.assertTrue(all(t.parent_id == parent.task_id for t in children))
        self.assertTrue(all(t.acceptance_report.task_id == t.task_id for t in children))
        events = load_events(self.run_dir)
        registered = {e["task_id"] for e in events if e["kind"] == "task_registered"}
        self.assertEqual(registered, {t.task_id for t in result.subtasks})
        ended = [e["task_id"] for e in events if e["kind"] == "subtask_end"]
        self.assertCountEqual(ended, registered)
        board = json.loads((Path(result.workspace) / "board.json").read_text())
        self.assertEqual(len(board["subtasks"]), 3)

    def test_invalid_planning_attempts_have_distinct_raw_usage_events(self) -> None:
        provider = TaskAwareProvider(_plan(), {"goal": _writes()})
        result = self._orchestrator(provider).run("goal", goal_id="degraded")
        self.assertTrue(result.succeeded)
        events = load_events(self.run_dir)
        usages = [e for e in events if e["kind"] == "plan_usage"]
        self.assertEqual(len(usages), 2)
        self.assertEqual(len({e["call_id"] for e in usages}), 2)
        self.assertTrue(all(e["root_id"] == e["task_id"] == "degraded" for e in usages))
        self.assertTrue(all(e["attempt_id"] == result.attempt_id for e in usages))
        self.assertEqual(sum(e["prompt_tokens"] + e["completion_tokens"] for e in usages), 60)
        self.assertEqual(result.tokens, 120)


if __name__ == "__main__":
    unittest.main()
