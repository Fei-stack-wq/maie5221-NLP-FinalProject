"""Orchestration commits identities and terminal states before projecting them."""
from __future__ import annotations

import unittest
from pathlib import Path

from miniagent.acceptance import evaluate
from miniagent.config import Limits, Settings
from miniagent.orchestrator import Orchestrator
from miniagent.schemas import RunResult
from miniagent.task_service import TaskService
from miniagent.task_store import StoreError, TaskStore
from tests.support import cleanup, make_temp_dir
from tests.test_orchestrator import ROSTER, TaskAwareProvider, _plan, _subtask


def accepted(_workspace):
    return True, "host check passed"


class TaskStoreOrchestratorTest(unittest.TestCase):
    def setUp(self):
        self.base = make_temp_dir("task-store-orchestrator")
        self.run_dir = self.base / "run"

    def tearDown(self):
        cleanup(self.base)

    def orchestrator(self, provider=None, **kwargs):
        provider = provider or TaskAwareProvider(_plan(_subtask("one", "first")), {"first": [{"text": "answer"}]})
        kwargs.setdefault("default_acceptance", accepted)
        kwargs.setdefault("root_acceptance", accepted)
        orchestrator = Orchestrator(provider=provider, settings=kwargs.pop("settings", Settings()),
                                    roster=ROSTER, run_dir=kwargs.pop("run_dir", self.run_dir), **kwargs)
        self.addCleanup(orchestrator.close)
        return orchestrator

    def assert_closed(self, store, result):
        for task in store.tasks(root_id=result.root_id):
            self.assertIn(task.status, {"completed", "unverified", "failed", "cancelled"})
            for attempt in store.attempts(task.task_id):
                self.assertIn(attempt.state, {"succeeded", "unverified", "failed", "cancelled"})
                runs = store.runs(attempt.attempt_id)
                self.assertEqual(len(runs), 1)
                self.assertEqual(runs[0].state, "ended")
                self.assertIsNotNone(runs[0].ended_at)

    def test_every_scheduler_persists_root_children_reports_and_execution_ids(self):
        for scheduler in ("serial", "parallel", "evented"):
            with self.subTest(scheduler=scheduler):
                provider = TaskAwareProvider(_plan(_subtask("one", "first"), _subtask("two", "second", deps=["one"])), {})
                orchestrator = self.orchestrator(provider, run_dir=self.run_dir / scheduler)
                result = orchestrator.run("persist graph", task_id=f"root-{scheduler}", scheduler=scheduler)
                self.assertTrue(result.succeeded)
                reopened = TaskStore(self.run_dir / scheduler / "tasks.sqlite3")
                self.assert_closed(reopened, result)
                records = reopened.tasks(root_id=result.root_id)
                self.assertEqual(len(records), 3)
                for projection in [result, *result.subtasks]:
                    task = reopened.get(projection.task_id)
                    self.assertEqual(task.status, "completed")
                    self.assertEqual(task.current_attempt_id, projection.attempt_id)
                    self.assertEqual(task.latest_report_id, projection.acceptance_report.report_id)
                    self.assertEqual(reopened.runs(projection.attempt_id)[0].run_id, projection.run_id)
                    attachments = [event for event in reopened.events(task_id=projection.task_id)
                                   if event["kind"] == "execution_attached"]
                    self.assertEqual(len(attachments), 1)
                    self.assertEqual(attachments[0]["payload"]["run_id"], projection.run_id)
                    self.assertFalse((Path(projection.workspace) / "tasks.sqlite3").exists())
                self.assertEqual(reopened.get(f"{result.root_id}:two").deps, (f"{result.root_id}:one",))

    def test_plan_is_fully_registered_before_first_agent_starts(self):
        provider = TaskAwareProvider(_plan(_subtask("one", "first"), _subtask("two", "second")), {})
        orchestrator = self.orchestrator(provider)
        original = orchestrator._build_agent
        observed = []

        def factory(task):
            records = orchestrator.task_service.store.children(task.root_id)
            observed.append({record.task_id for record in records})
            self.assertEqual(orchestrator.task_service.store.get(task.root_id).status, "running")
            return original(task)

        orchestrator.agent_factory = factory
        result = orchestrator.run("atomic plan", task_id="root")
        self.assertTrue(result.succeeded)
        self.assertEqual(observed, [{"root:one", "root:two"}] * 2)

    def test_failure_cancels_only_dependents_without_attempts(self):
        for scheduler in ("serial", "parallel", "evented"):
            with self.subTest(scheduler=scheduler):
                provider = TaskAwareProvider(_plan(_subtask("one", "first"), _subtask("two", "blocked", deps=["one"]),
                                                   _subtask("three", "independent")), {})
                orchestrator = self.orchestrator(provider, run_dir=self.run_dir / scheduler,
                                                acceptances={"one": lambda _workspace: (False, "missing output")})
                result = orchestrator.run("failure branch", scheduler=scheduler)
                store = orchestrator.task_service.store
                self.assert_closed(store, result)
                self.assertEqual(store.get(f"{result.root_id}:one").status, "failed")
                blocked = store.get(f"{result.root_id}:two")
                self.assertEqual(blocked.status, "cancelled")
                self.assertIn("上游", blocked.reason)
                self.assertEqual(store.attempts(blocked.task_id), [])
                self.assertEqual(store.get(f"{result.root_id}:three").status, "completed")
                self.assertEqual(store.get(result.root_id).status, "failed")

    def test_dispatch_exception_cancels_registered_plan_before_root_closes(self):
        provider = TaskAwareProvider(_plan(_subtask("one", "first"), _subtask("two", "second")), {})
        orchestrator = self.orchestrator(provider)

        def broken_dispatch(tasks, board):
            raise RuntimeError("dispatch failed before work")

        orchestrator._dispatch_serial = broken_dispatch
        result = orchestrator.run("interrupted plan")
        self.assertEqual(result.status, "error")
        store = orchestrator.task_service.store
        self.assert_closed(store, result)
        for child in store.children(result.root_id):
            self.assertEqual(child.status, "cancelled")
            self.assertIn("dispatch failed", child.reason)
            self.assertEqual(store.attempts(child.task_id), [])

    def test_agent_factory_exception_closes_started_child_and_root(self):
        def broken_factory(task):
            raise RuntimeError("agent construction failed")

        orchestrator = self.orchestrator(agent_factory=broken_factory)
        result = orchestrator.run("startup failure")
        child = result.subtasks[0]
        self.assertEqual(child.status, "error")
        self.assertIn("agent construction failed", child.error)
        self.assert_closed(orchestrator.task_service.store, result)
        self.assertEqual(orchestrator.task_service.store.get(child.task_id).status, "failed")
        self.assertTrue(child.attempt_id)
        self.assertTrue(child.run_id)

    def test_budget_skipped_children_are_cancelled_without_attempts(self):
        settings = Settings(limits=Limits(max_total_tokens=0))
        orchestrator = self.orchestrator(settings=settings)
        result = orchestrator.run("no execution budget")
        child = result.subtasks[0]
        record = orchestrator.task_service.store.get(child.task_id)
        self.assertEqual(child.status, "skipped")
        self.assertEqual(record.status, "cancelled")
        self.assertIn("预算", record.reason)
        self.assertEqual(orchestrator.task_service.store.attempts(child.task_id), [])
        self.assert_closed(orchestrator.task_service.store, result)

    def test_legacy_custom_agent_without_execution_parameter_gets_unverified_terminal(self):
        calls = []

        class LegacyAgent:
            def run(self, objective, *, task_id, root_id, parent_task_id, attempt_id, acceptance, budget, on_step):
                calls.append(task_id)
                return RunResult(task_id=task_id, attempt_id=attempt_id, agent="legacy", status="completed", answer="unsupported claim")

        orchestrator = self.orchestrator(agent_factory=lambda task: LegacyAgent())
        result = orchestrator.run("legacy agent")
        child = result.subtasks[0]
        self.assertEqual(calls, [child.task_id])
        self.assertEqual(child.status, "unverified")
        self.assertEqual(child.acceptance_report.verdict, "unverified")
        self.assertEqual(orchestrator.task_service.store.get(child.task_id).status, "unverified")
        self.assert_closed(orchestrator.task_service.store, result)

    def test_legacy_custom_agent_with_real_acceptance_receipts_is_persisted_completed(self):
        workspace = self.base / "legacy-workspace"
        workspace.mkdir()
        calls = []
        attachments_seen = []

        class LegacyAgent:
            def run(self, objective, *, task_id, root_id, parent_task_id, attempt_id, acceptance, budget, on_step):
                attachments_seen.append(sum(event["kind"] == "execution_attached"
                                            for event in orchestrator.task_service.store.events(task_id=task_id)))
                evaluation = evaluate(acceptance, workspace, task_id=task_id,
                                      attempt_id=attempt_id, objective=objective)
                result = RunResult(task_id=task_id, attempt_id=attempt_id, agent="legacy", status="completed",
                                   answer="verified legacy answer", sandbox=str(workspace),
                                   acceptance_spec=evaluation.spec, acceptance_report=evaluation.report,
                                   acceptance_evidence=evaluation.trace_payload()["receipts"])
                calls.append(result)
                return result

        orchestrator = self.orchestrator(agent_factory=lambda task: LegacyAgent())
        result = orchestrator.run("valid legacy completion")
        self.assertEqual(len(calls), 1)
        self.assertEqual(attachments_seen, [1])
        self.assertTrue(calls[0].succeeded)
        self.assertTrue(result.succeeded)
        child = result.subtasks[0]
        self.assertEqual(child.status, "done")
        self.assertEqual(child.error, "")
        self.assertEqual(child.acceptance_report, calls[0].acceptance_report)
        self.assertEqual(child.acceptance_evidence, calls[0].acceptance_evidence)
        self.assertEqual(orchestrator.task_service.store.get(child.task_id).status, "completed")
        self.assert_closed(orchestrator.task_service.store, result)

    def test_losing_child_claim_does_not_close_the_other_execution(self):
        orchestrator = self.orchestrator()
        service = orchestrator.task_service
        original_start = service.start
        winners = []

        def race_start(task_id, **kwargs):
            if task_id == "claim-root:one":
                winners.append(original_start(task_id, expected_revision=kwargs["expected_revision"],
                                              context={"runtime": "another executor"}))
            return original_start(task_id, **kwargs)

        service.start = race_start
        with self.assertRaises(StoreError) as caught:
            orchestrator.run("claim race", task_id="claim-root")
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(len(winners), 1)
        winner = winners[0]
        stored = service.store.get(winner.task.task_id)
        self.assertEqual(stored.status, "running")
        self.assertEqual(stored.current_attempt_id, winner.attempt.attempt_id)
        self.assertEqual(service.store.get_record("AttemptRecord", winner.attempt.attempt_id).state, "running")
        self.assertEqual(service.store.get_record("RunRecord", winner.run.run_id).state, "running")
        self.assertEqual(service.store.get("claim-root").status, "running")
        with self.assertRaises(StoreError):
            orchestrator._end_subtask(orchestrator._all_tasks[0])
        self.assertEqual(service.store.get_record("RunRecord", winner.run.run_id).state, "running")

    def test_factory_attachment_winner_is_not_closed_after_coordinator_attach_conflict(self):
        orchestrator = self.orchestrator()
        service = orchestrator.task_service
        winners = []
        calls = []

        class CustomAgent:
            def run(self, objective, **kwargs):
                calls.append(objective)
                raise AssertionError("losing executor must not enter run")

        def factory(task):
            handle = orchestrator._executions[task.task_id]
            service.attach_execution(handle)
            winners.append(handle)
            return CustomAgent()

        orchestrator.agent_factory = factory
        with self.assertRaises(StoreError) as caught:
            orchestrator.run("attachment race", task_id="attachment-root")
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(calls, [])
        self.assertEqual(len(winners), 1)
        winner = winners[0]
        self.assertNotIn(winner.task.task_id, orchestrator._executions)
        self.assertEqual(service.store.get(winner.task.task_id).status, "running")
        self.assertEqual(service.store.get_record("AttemptRecord", winner.attempt.attempt_id).state, "running")
        self.assertEqual(service.store.get_record("RunRecord", winner.run.run_id).state, "running")
        self.assertEqual(service.store.get("attachment-root").status, "running")
        with self.assertRaises(StoreError):
            orchestrator._end_subtask(orchestrator._all_tasks[0])
        self.assertEqual(service.store.get_record("RunRecord", winner.run.run_id).state, "running")

    def test_custom_agent_typeerror_is_not_retried_as_signature_fallback(self):
        calls = []

        class CustomAgent:
            def run(self, objective, **kwargs):
                calls.append(kwargs["execution"].run.run_id)
                raise TypeError("failure inside an agent")

        orchestrator = self.orchestrator(agent_factory=lambda task: CustomAgent())
        result = orchestrator.run("one invocation")
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.subtasks[0].status, "error")
        self.assert_closed(orchestrator.task_service.store, result)

    def test_persisted_report_and_status_override_a_custom_agents_conflicting_return(self):
        orchestrator = self.orchestrator()
        workspace = self.base / "custom-workspace"
        workspace.mkdir()
        persisted = []

        class CustomAgent:
            def run(self, objective, **kwargs):
                handle = kwargs["execution"]
                unverified = evaluate(None, workspace, task_id=handle.task.task_id,
                                      attempt_id=handle.attempt.attempt_id, objective=objective)
                orchestrator.task_service.finish(
                    handle.task.task_id, expected_revision=handle.task.revision,
                    status="unverified", report=unverified.report,
                )
                persisted.append(unverified.report.report_id)
                claimed = evaluate(accepted, workspace, task_id=handle.task.task_id,
                                   attempt_id=handle.attempt.attempt_id, objective=objective)
                return RunResult(task_id=handle.task.task_id, attempt_id=handle.attempt.attempt_id,
                                 run_id=handle.run.run_id, agent="custom", status="completed",
                                 acceptance_spec=claimed.spec, acceptance_report=claimed.report,
                                 acceptance_evidence=claimed.trace_payload()["receipts"])

        orchestrator.agent_factory = lambda task: CustomAgent()
        result = orchestrator.run("authoritative result")
        child = result.subtasks[0]
        self.assertEqual(child.status, "unverified")
        self.assertEqual(child.acceptance_report.report_id, persisted[0])
        self.assertIsNone(child.acceptance_spec)
        self.assertFalse(result.succeeded)

    def test_mutating_board_projection_cannot_unlock_a_dependency(self):
        provider = TaskAwareProvider(_plan(_subtask("one", "first"), _subtask("two", "second", deps=["one"])), {})
        orchestrator = self.orchestrator(provider, default_acceptance=None)

        def tampered_dispatch(tasks, board):
            orchestrator._execute(tasks[0], board)
            tasks[0].status = "done"
            if not orchestrator._block_if_deps_failed(tasks[1], tasks):
                orchestrator._execute(tasks[1], board)

        orchestrator._dispatch_serial = tampered_dispatch
        result = orchestrator.run("projection is not authority")
        self.assertEqual([task.status for task in result.subtasks], ["unverified", "blocked"])
        blocked = orchestrator.task_service.store.get(result.subtasks[1].task_id)
        self.assertEqual(blocked.status, "cancelled")
        self.assertEqual(orchestrator.task_service.store.attempts(blocked.task_id), [])

    def test_closed_root_cannot_implicitly_rerun_in_same_or_reopened_orchestrator(self):
        orchestrator = self.orchestrator()
        first = orchestrator.run("persist once", task_id="closed-root")
        self.assertTrue(first.succeeded)
        for runtime in (orchestrator, self.orchestrator()):
            with self.assertRaises(StoreError):
                runtime.run("persist once", task_id="closed-root")
        store = TaskStore(self.run_dir / "tasks.sqlite3")
        self.assertEqual(len(store.attempts(first.task_id)), 1)
        self.assertEqual(len(store.tasks(root_id=first.task_id)), 2)

    def test_no_run_directory_still_uses_control_store_outside_agent_workspaces(self):
        orchestrator = self.orchestrator(run_dir=None)
        self.addCleanup(cleanup, orchestrator.control_dir)
        result = orchestrator.run("ephemeral control directory")
        self.assertTrue(result.succeeded)
        self.assertTrue((orchestrator.control_dir / "tasks.sqlite3").is_file())
        self.assertNotEqual(orchestrator.control_dir, Path(result.workspace))
        self.assertFalse((orchestrator.control_dir / "tasks.sqlite3").is_relative_to(Path(result.subtasks[0].workspace)))

    def test_injected_task_service_is_shared_with_default_agents(self):
        service = TaskService(TaskStore(self.base / "control" / "shared.sqlite3"))
        orchestrator = self.orchestrator(task_service=service)
        result = orchestrator.run("injected control store")
        self.assertTrue(result.succeeded)
        self.assertFalse((self.run_dir / "tasks.sqlite3").exists())
        self.assertEqual(len(service.store.tasks(root_id=result.root_id)), 2)
        self.assert_closed(service.store, result)

    def test_dynamic_child_ids_are_persistent_uuids_and_same_action_does_not_rerun(self):
        provider = TaskAwareProvider(_plan(_subtask("parent", "parent work")), {
            "parent work": [
                {"tool_calls": [{"name": "delegate_task", "arguments": {"objective": "child work"}}]},
                {"text": "parent answer"},
            ],
            "child work": [{"text": "child answer"}],
        })
        orchestrator = self.orchestrator(provider, enable_delegation=True)
        original = orchestrator._handle_delegate
        replays = []
        actions = []

        def duplicate_delivery(payload):
            actions.append(payload["__runtime_action_id"])
            first = original(payload)
            second = original(dict(payload))
            replays.append((first.ok, second.ok))
            return first

        orchestrator._handle_delegate = duplicate_delivery
        result = orchestrator.run("delegate once", task_id="root")
        self.assertTrue(result.succeeded)
        self.assertEqual(replays, [(True, True)])
        self.assertEqual(len(result.subtasks), 2)
        parent, child = result.subtasks
        self.assertTrue(child.id.startswith("parent.d1."))
        self.assertNotEqual(child.task_id, f"{result.root_id}:{child.id}")
        self.assertEqual(child.parent_id, parent.task_id)
        store = TaskStore(self.run_dir / "tasks.sqlite3")
        self.assertEqual(len(store.attempts(child.task_id)), 1)
        self.assertRegex(child.task_id, r"^task-[0-9a-f]{32}$")
        parent_record = store.get(parent.task_id)
        replayed = TaskService(store).create(
            objective=child.objective, creation_key=f"delegation:{parent.task_id}:{actions[0]}",
            parent_id=parent.task_id, owner=parent_record.owner_profile_id,
            assignee=child.assignee, project_id=parent_record.project_id,
        )
        self.assertEqual(replayed.task_id, child.task_id)
        self.assert_closed(store, result)


if __name__ == "__main__":
    unittest.main()
