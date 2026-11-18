"""Durable lifecycle service tests with real SQLite and independent file checks."""
from __future__ import annotations

import multiprocessing
import unittest
from pathlib import Path
from unittest.mock import patch

from miniagent.acceptance import accept_files, bind, evaluate
from miniagent.task_service import TaskService
from miniagent.task_store import StoreError, StoreTransaction, TaskStore
from tests.support import cleanup, make_temp_dir


def _claim_worker(path: str, task_id: str, revision: int, barrier, queue) -> None:
    service = TaskService(TaskStore(path))
    barrier.wait(timeout=10)
    try:
        handle = service.start(task_id, expected_revision=revision, context={"worker": "claimant"})
        queue.put(("started", handle.attempt.attempt_id, handle.run.run_id))
    except StoreError as exc:
        queue.put(("rejected", exc.code))


def _attach_worker(path: str, handle, barrier, queue) -> None:
    service = TaskService(TaskStore(path))
    barrier.wait(timeout=10)
    try:
        service.attach_execution(handle)
        queue.put(("attached",))
    except StoreError as exc:
        queue.put(("rejected", exc.code))


class TaskServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.base = make_temp_dir("task-service")
        self.path = self.base / "tasks.sqlite3"
        self.store = TaskStore(self.path)
        self.service = TaskService(self.store)
        self.workspace = self.base / "products"
        self.workspace.mkdir()

    def tearDown(self) -> None:
        cleanup(self.base)

    def create(self, task_id="root", **kwargs):
        values = dict(task_id=task_id, objective="produce verified content", creation_key=f"create:{task_id}",
                      owner="owner", assignee="worker")
        values.update(kwargs)
        return self.service.create(**values)

    def start(self, task=None):
        task = task or self.create()
        return self.service.start(task.task_id, expected_revision=task.revision, context={"workspace": str(self.workspace)})

    def evaluation(self, handle, *, passed=True, acceptance=None):
        (self.workspace / "result.txt").write_text("verified content" if passed else "wrong content")
        checker = acceptance or accept_files("result.txt", min_bytes=1, contains={"result.txt": "verified content"})
        return evaluate(checker, self.workspace, task_id=handle.task.task_id,
                        attempt_id=handle.attempt.attempt_id, objective=handle.task.objective)

    def finish(self, handle, outcome, status="completed"):
        return self.service.finish(handle.task.task_id, expected_revision=self.store.get(handle.task.task_id).revision,
            status=status, spec=outcome.spec, report=outcome.report, receipts=outcome.trace_payload()["receipts"])

    def test_create_replay_survives_reopening_without_duplicate_events(self) -> None:
        original = self.service.create(objective="same", creation_key="request", owner="owner", assignee="worker")
        reopened = TaskService(TaskStore(self.path))
        again = reopened.create(objective="same", creation_key="request", owner="owner", assignee="worker")
        self.assertEqual(original, again)
        self.assertEqual(len(self.store.tasks()), 1)
        self.assertEqual(len(self.store.events()), 1)
        constraints = self.store.get_blob(original.constraints_ref.id)
        self.assertTrue(constraints["runtime_context_only"])
        self.assertEqual(constraints["version"], original.constraints_ref.version)
        self.assertEqual(original.status, "created")
        self.assertIsNone(original.current_attempt_id)

    def test_creation_key_is_project_scoped_and_semantically_bound(self) -> None:
        first = self.create(creation_key="same-key", project_id="one")
        second = self.create("other", creation_key="same-key", project_id="two")
        self.assertNotEqual(first.creation_key, second.creation_key)
        for overrides in ({"objective": "changed"}, {"assignee": "another"}, {"owner": "another"}, {"task_id": "different"}):
            with self.subTest(overrides=overrides):
                args = dict(task_id="root", creation_key="same-key", project_id="one")
                args.update(overrides)
                with self.assertRaises(StoreError) as raised:
                    self.create(**args)
                self.assertEqual(raised.exception.code, "conflict")
        self.assertEqual(len(self.store.tasks()), 2)

    def test_different_creation_keys_cannot_overwrite_same_task_id(self) -> None:
        original = self.create()
        with self.assertRaises(StoreError):
            self.create(creation_key="unrelated")
        self.assertEqual(self.store.get("root"), original)
        self.assertEqual(len(self.store.events()), 1)

    def test_plan_forward_dependencies_and_replay_are_atomic(self) -> None:
        parent = self.create()
        definitions = [{"id": "s2", "objective": "second", "assignee": "writer", "deps": ["s1"]},
                       {"id": "s1", "objective": "first", "assignee": "worker", "deps": []}]
        tasks = self.service.create_plan(parent.task_id, definitions)
        self.assertEqual([t.task_id for t in tasks], ["root:s2", "root:s1"])
        self.assertEqual(tasks[0].deps, ("root:s1",))
        events = self.store.events()
        replay = TaskService(TaskStore(self.path)).create_plan(parent.task_id, list(reversed(definitions)))
        self.assertEqual({t.task_id for t in replay}, {t.task_id for t in tasks})
        self.assertEqual(self.store.events(), events)
        with self.assertRaises(StoreError) as raised:
            self.service.create_plan(parent.task_id, definitions[:1])
        self.assertIn(raised.exception.code, {"conflict", "integrity_error"})

    def test_cyclic_plan_rolls_back_every_task_binding_and_event(self) -> None:
        self.create()
        definitions = [{"id": "a", "objective": "a", "assignee": "worker", "deps": ["b"]},
                       {"id": "b", "objective": "b", "assignee": "worker", "deps": ["a"]}]
        with self.assertRaises(StoreError):
            self.service.create_plan("root", definitions)
        self.assertEqual([task.task_id for task in self.store.tasks()], ["root"])
        self.assertEqual(len(self.store.events()), 1)
        definitions[1]["deps"] = []
        self.assertEqual(len(self.service.create_plan("root", definitions)), 2)

    def test_cross_root_dependency_and_self_dependency_are_rejected(self) -> None:
        self.create("other")
        with self.assertRaises(StoreError):
            self.create(deps=["other"])
        with self.assertRaises(StoreError):
            self.create(deps=["root"])
        self.assertEqual([t.task_id for t in self.store.tasks()], ["other"])

    def test_start_atomically_returns_running_task_attempt_run_and_context(self) -> None:
        task = self.create()
        handle = self.start(task)
        self.assertEqual(handle.task.revision, task.revision + 2)
        self.assertEqual(handle.task.status, "running")
        self.assertEqual(handle.attempt.state, "running")
        self.assertEqual(handle.run.state, "running")
        self.assertEqual(handle.task.current_attempt_id, handle.attempt.attempt_id)
        self.assertEqual(handle.run.attempt_id, handle.attempt.attempt_id)
        self.assertEqual(self.store.get_blob(handle.attempt.context_ref)["context"], {"workspace": str(self.workspace)})
        reopened = TaskStore(self.path)
        self.assertEqual(reopened.get("root"), handle.task)
        self.assertEqual(reopened.attempts("root"), [handle.attempt])
        self.assertEqual(reopened.runs(handle.attempt.attempt_id), [handle.run])

    def test_start_uses_explicit_execution_ids_and_ready_cas_revision(self) -> None:
        task = self.create()
        ready = self.service.transition(task.task_id, expected_revision=task.revision, status="ready")
        with self.assertRaises(StoreError) as raised:
            self.service.start(task.task_id, expected_revision=task.revision, context={})
        self.assertEqual(raised.exception.code, "revision_conflict")
        handle = self.service.start(task.task_id, expected_revision=ready.revision, context={}, attempt_id="attempt-id", run_id="run-id")
        self.assertEqual(handle.task.revision, ready.revision + 1)
        self.assertEqual(handle.attempt.attempt_id, "attempt-id")
        self.assertEqual(handle.run.run_id, "run-id")

    def test_start_requires_running_parent_and_completed_dependencies(self) -> None:
        root = self.create()
        children = self.service.create_plan(root.task_id, [
            {"id": "a", "objective": "first", "assignee": "worker", "deps": []},
            {"id": "b", "objective": "second", "assignee": "worker", "deps": ["a"]},
        ])
        with self.assertRaises(StoreError):
            self.start(children[0])
        self.start(root)
        with self.assertRaises(StoreError):
            self.start(children[1])
        first = self.start(children[0])
        self.finish(first, self.evaluation(first))
        self.assertEqual(self.start(children[1]).task.status, "running")

    def test_fault_during_start_leaves_no_partial_claim_or_events(self) -> None:
        task = self.create()
        emit = StoreTransaction.emit
        def fail_started(tx, kind, task_id, payload):
            if kind == "task_started":
                raise RuntimeError("injected outbox failure")
            return emit(tx, kind, task_id, payload)
        with patch.object(StoreTransaction, "emit", fail_started):
            with self.assertRaises(RuntimeError):
                self.start(task)
        self.assertEqual(self.store.get(task.task_id), task)
        self.assertEqual(self.store.attempts(task.task_id), [])
        self.assertEqual(len(self.store.events()), 1)
        self.assertEqual(self.start(task).task.status, "running")

    def test_two_processes_cannot_both_claim_the_same_revision(self) -> None:
        task = self.create()
        ctx = multiprocessing.get_context("spawn")
        barrier, queue = ctx.Barrier(2), ctx.Queue()
        workers = [ctx.Process(target=_claim_worker, args=(str(self.path), task.task_id, task.revision, barrier, queue)) for _ in range(2)]
        try:
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=15)
                self.assertEqual(worker.exitcode, 0)
            outcomes = [queue.get(timeout=2) for _ in workers]
            self.assertEqual(sum(item[0] == "started" for item in outcomes), 1)
            self.assertEqual(sum(item == ("rejected", "revision_conflict") for item in outcomes), 1)
            self.assertEqual(len(self.store.attempts(task.task_id)), 1)
            self.assertEqual(len([e for e in self.store.events() if e["kind"] == "task_started"]), 1)
        finally:
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                worker.join(timeout=2)
            queue.close()

    def test_passed_completion_requires_actual_receipt_closure(self) -> None:
        handle = self.start()
        outcome = self.evaluation(handle)
        with self.assertRaises(StoreError):
            self.service.finish("root", expected_revision=handle.task.revision, status="completed", spec=outcome.spec, report=outcome.report)
        self.assertEqual(self.store.get("root"), handle.task)
        self.assertEqual(self.store.attempts("root")[0].state, "running")
        self.assertEqual(self.store.runs(handle.attempt.attempt_id)[0].state, "running")
        finished = self.finish(handle, outcome)
        self.assertEqual(finished.status, "completed")
        self.assertEqual(finished.constraints_ref, outcome.spec.constraints_ref)
        self.assertEqual(self.store.attempts("root")[0].state, "succeeded")
        self.assertEqual(self.store.runs(handle.attempt.attempt_id)[0].state, "ended")
        receipts = self.store.get_blob("acceptance-evidence:" + finished.latest_report_id)["receipts"]
        self.assertEqual(receipts, outcome.trace_payload()["receipts"])
        self.assertEqual(len([e for e in self.store.events() if e["kind"] == "acceptance_constraints_bound"]), 1)

    def test_execution_handle_attachment_is_durable_one_time_and_revision_neutral(self) -> None:
        handle = self.start()
        self.service.attach_execution(handle)
        self.assertEqual(self.store.get("root"), handle.task)
        with self.assertRaises(StoreError) as raised:
            TaskService(TaskStore(self.path)).attach_execution(handle)
        self.assertEqual(raised.exception.code, "conflict")
        self.assertEqual(len([e for e in self.store.events() if e["kind"] == "execution_attached"]), 1)

    def test_two_processes_cannot_attach_the_same_execution_handle(self) -> None:
        handle = self.start()
        ctx = multiprocessing.get_context("spawn")
        barrier, queue = ctx.Barrier(2), ctx.Queue()
        workers = [ctx.Process(target=_attach_worker, args=(str(self.path), handle, barrier, queue)) for _ in range(2)]
        try:
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=15)
                self.assertEqual(worker.exitcode, 0)
            outcomes = [queue.get(timeout=2) for _ in workers]
            self.assertCountEqual(outcomes, [("attached",), ("rejected", "conflict")])
            self.assertEqual(self.store.get("root").revision, handle.task.revision)
            self.assertEqual(len([e for e in self.store.events() if e["kind"] == "execution_attached"]), 1)
        finally:
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                worker.join(timeout=2)
            queue.close()

    def test_report_from_another_task_or_attempt_cannot_complete(self) -> None:
        handle = self.start()
        other = self.start(self.create("other"))
        outcome = self.evaluation(other)
        with self.assertRaises(StoreError):
            self.finish(handle, outcome)
        self.assertEqual(self.store.get("root").status, "running")
        own = self.evaluation(handle)
        corrupted = own.report.model_copy(update={"attempt_id": other.attempt.attempt_id})
        with self.assertRaises(StoreError):
            self.service.finish("root", expected_revision=handle.task.revision, status="completed", spec=own.spec,
                                report=corrupted, receipts=own.trace_payload()["receipts"])

    def test_unverified_requires_matching_report_and_keeps_candidate_history(self) -> None:
        handle = self.start()
        with self.assertRaises(StoreError):
            self.service.finish("root", expected_revision=handle.task.revision, status="unverified")
        outcome = evaluate(None, self.workspace, task_id="root", attempt_id=handle.attempt.attempt_id, objective="goal")
        finished = self.finish(handle, outcome, status="unverified")
        self.assertEqual(finished.status, "unverified")
        self.assertIsNone(finished.acceptance_ref)
        self.assertEqual(self.store.attempts("root")[0].report_ids, (outcome.report.report_id,))

    def test_fatal_execution_error_can_preserve_passed_report_without_success(self) -> None:
        handle = self.start()
        outcome = self.evaluation(handle)
        with self.assertRaises(StoreError):
            self.service.finish("root", expected_revision=handle.task.revision, status="completed", error="fatal",
                spec=outcome.spec, report=outcome.report, receipts=outcome.trace_payload()["receipts"])
        finished = self.service.finish("root", expected_revision=handle.task.revision, status="failed", error="fatal",
            spec=outcome.spec, report=outcome.report, receipts=outcome.trace_payload()["receipts"])
        self.assertEqual(finished.status, "failed")
        self.assertEqual(self.store.get_record("AcceptanceReport", finished.latest_report_id).verdict, "passed")

    def test_failed_execution_without_report_closes_all_three_records(self) -> None:
        handle = self.start()
        finished = self.service.finish("root", expected_revision=handle.task.revision, status="failed", error="provider unavailable")
        self.assertEqual(finished.status, "failed")
        self.assertIsNone(finished.latest_report_id)
        self.assertEqual(self.store.attempts("root")[0].state, "failed")
        self.assertEqual(self.store.runs(handle.attempt.attempt_id)[0].state, "ended")

    def test_repair_reports_persist_and_latest_cannot_move_backwards(self) -> None:
        handle = self.start()
        binding = bind(accept_files("result.txt", min_bytes=1, contains={"result.txt": "verified content"}),
                       self.workspace, task_id="root", objective=handle.task.objective)
        first = self.evaluation(handle, passed=False, acceptance=binding)
        task = self.service.transition("root", expected_revision=handle.task.revision, status="verifying")
        task = self.service.record_acceptance("root", expected_revision=task.revision, spec=first.spec,
                    report=first.report, receipts=first.trace_payload()["receipts"])
        task = self.service.transition("root", expected_revision=task.revision, status="running")
        second = self.evaluation(handle, acceptance=binding)
        task = self.service.transition("root", expected_revision=task.revision, status="verifying")
        task = self.service.record_acceptance("root", expected_revision=task.revision, spec=second.spec,
                    report=second.report, receipts=second.trace_payload()["receipts"])
        before = self.store.events()
        replay = self.service.record_acceptance("root", expected_revision=task.revision, spec=second.spec,
                    report=second.report, receipts=second.trace_payload()["receipts"])
        self.assertEqual(replay, task)
        self.assertEqual(self.store.events(), before)
        with self.assertRaises(StoreError) as raised:
            self.service.record_acceptance("root", expected_revision=task.revision, spec=first.spec,
                        report=first.report, receipts=first.trace_payload()["receipts"])
        self.assertEqual(raised.exception.code, "conflict")
        self.assertEqual(self.store.get("root").latest_report_id, second.report.report_id)
        delayed = first.report.model_copy(update={"report_id": "delayed-report-with-new-id"})
        with self.assertRaises(StoreError) as raised:
            self.service.record_acceptance("root", expected_revision=task.revision, spec=first.spec,
                        report=delayed, receipts=first.trace_payload()["receipts"])
        self.assertEqual(raised.exception.code, "conflict")
        finished = self.service.finish("root", expected_revision=task.revision, status="completed")
        self.assertEqual(finished.status, "completed")
        self.assertEqual(self.store.attempts("root")[0].report_ids, (first.report.report_id, second.report.report_id))

    def test_foreign_constraints_cannot_be_silently_rebound(self) -> None:
        handle = self.start()
        outcome = self.evaluation(handle)
        unsupported = outcome.spec.model_copy(update={"authority_ref": "external-authority", "params": {}})
        with self.assertRaises(StoreError) as raised:
            self.service.finish("root", expected_revision=handle.task.revision, status="completed", spec=unsupported,
                                report=outcome.report, receipts=outcome.trace_payload()["receipts"])
        self.assertEqual(raised.exception.code, "conflict")
        self.assertEqual(self.store.get("root").constraints_ref, handle.task.constraints_ref)

    def test_active_attempt_cannot_switch_spec_after_first_report(self) -> None:
        handle = self.start()
        first, second = self.evaluation(handle), self.evaluation(handle)
        task = self.service.transition("root", expected_revision=handle.task.revision, status="verifying")
        task = self.service.record_acceptance("root", expected_revision=task.revision, spec=first.spec,
            report=first.report, receipts=first.trace_payload()["receipts"])
        with self.assertRaises(StoreError) as raised:
            self.service.record_acceptance("root", expected_revision=task.revision, spec=second.spec,
                report=second.report, receipts=second.trace_payload()["receipts"])
        self.assertEqual(raised.exception.code, "conflict")

    def test_parent_cannot_finish_with_open_descendants(self) -> None:
        root = self.start()
        child = self.create("child", parent_id="root")
        with self.assertRaises(StoreError):
            self.service.finish("root", expected_revision=root.task.revision, status="failed", error="stop")
        self.service.cancel(child.task_id, expected_revision=child.revision, reason="parent interrupted")
        self.assertEqual(self.service.finish("root", expected_revision=root.task.revision, status="failed", error="stop").status, "failed")

    def test_cancellation_is_cas_guarded_and_only_for_unstarted_work(self) -> None:
        task = self.create()
        blocked = self.service.transition("root", expected_revision=task.revision, status="blocked", reason="dependency not ready")
        with self.assertRaises(StoreError) as raised:
            self.service.cancel("root", expected_revision=task.revision, reason="cancel")
        self.assertEqual(raised.exception.code, "revision_conflict")
        self.assertEqual(self.service.cancel("root", expected_revision=blocked.revision, reason="cancel").status, "cancelled")
        active = self.start(self.create("other"))
        with self.assertRaises(StoreError):
            self.service.cancel("other", expected_revision=active.task.revision, reason="cancel")

    def test_generic_transition_cannot_bypass_coupled_lifecycles(self) -> None:
        handle = self.start()
        for status in ("completed", "failed", "cancelled", "blocked", "waiting_user", "ready"):
            with self.subTest(status=status):
                with self.assertRaises(StoreError):
                    self.service.transition("root", expected_revision=handle.task.revision, status=status, reason="unsafe")
        self.assertEqual(self.store.get("root"), handle.task)

    def test_closed_work_does_not_implicitly_retry_or_create_more_children(self) -> None:
        handle = self.start()
        closed = self.service.finish("root", expected_revision=handle.task.revision, status="failed", error="stop")
        with self.assertRaises(StoreError):
            self.service.start("root", expected_revision=closed.revision, context={})
        with self.assertRaises(StoreError):
            self.create("child", parent_id="root")
        self.assertEqual(len(self.store.attempts("root")), 1)
        self.assertEqual(self.service.resume_candidates(), [])

    def test_action_identity_replay_is_durable_and_does_not_change_task_revision(self) -> None:
        handle = self.start()
        calls = [{"tool_call_id": "provider-call", "operation": "calculate", "arguments": {"x": 0.5}, "raw_arguments": '{"x":0.5}'}]
        identities = self.service.action_identities("root", attempt_id=handle.attempt.attempt_id,
                    run_id=handle.run.run_id, step=1, calls=calls)
        before = self.store.events()
        again = TaskService(TaskStore(self.path)).action_identities("root", attempt_id=handle.attempt.attempt_id,
                    run_id=handle.run.run_id, step=1, calls=calls)
        self.assertEqual(identities, again)
        self.assertEqual(self.store.events(), before)
        self.assertEqual(self.store.get("root"), handle.task)
        self.assertTrue(identities[0]["identity_only"])
        self.assertEqual(identities[0]["status"], "prepared")
        self.assertNotIn("authorization_ref", identities[0])
        self.assertNotIn("lease_id", identities[0])
        other = self.service.action_identities("root", attempt_id=handle.attempt.attempt_id,
                    run_id=handle.run.run_id, step=2, calls=calls)
        self.assertNotEqual(other[0]["action_id"], identities[0]["action_id"])

    def test_action_replay_rejects_changed_arguments_raw_text_or_batch_members(self) -> None:
        handle = self.start()
        original = [{"tool_call_id": "c", "operation": "read", "arguments": {}, "raw_arguments": "{}"}]
        args = dict(attempt_id=handle.attempt.attempt_id, run_id=handle.run.run_id, step=1)
        self.service.action_identities("root", calls=original, **args)
        for replacement in (
            [{**original[0], "arguments": {"x": 1}}], [{**original[0], "raw_arguments": "{ }"}],
            [*original, {"tool_call_id": "other", "operation": "read", "arguments": {}}], [],
        ):
            with self.subTest(replacement=replacement):
                with self.assertRaises(StoreError) as raised:
                    self.service.action_identities("root", calls=replacement, **args)
                self.assertEqual(raised.exception.code, "conflict")

    def test_action_identity_rejects_foreign_closed_and_invalid_requests(self) -> None:
        handle = self.start()
        calls = [{"tool_call_id": "c", "operation": "read", "arguments": {}}]
        args = dict(attempt_id=handle.attempt.attempt_id, run_id=handle.run.run_id, step=1, calls=calls)
        for changes in ({"attempt_id": "foreign"}, {"run_id": "foreign"}, {"step": True},
                        {"calls": calls * 2}, {"calls": [{**calls[0], "arguments": {"bad": float("nan")}}]}):
            with self.subTest(changes=changes):
                with self.assertRaises(StoreError):
                    self.service.action_identities("root", **{**args, **changes})
        self.service.finish("root", expected_revision=handle.task.revision, status="failed", error="stop")
        with self.assertRaises(StoreError):
            self.service.action_identities("root", **args)

    def test_resume_candidates_only_list_open_records_without_reclaiming(self) -> None:
        running = self.start()
        created = self.create("pending")
        reopened = TaskService(TaskStore(self.path))
        self.assertEqual({t.task_id for t in reopened.resume_candidates()}, {running.task.task_id, created.task_id})
        self.assertEqual(len(self.store.attempts("root")), 1)
        self.assertEqual(self.store.get("root").current_attempt_id, running.attempt.attempt_id)


if __name__ == "__main__":
    unittest.main()
