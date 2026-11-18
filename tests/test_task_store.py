"""SQLite durability, transaction boundaries and internal graph integrity."""
from __future__ import annotations

import multiprocessing
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from miniagent.contracts.acceptance import AcceptanceReport, AcceptanceSpec
from miniagent.contracts.tasks import AttemptRecord, Checkpoint, RunRecord, TaskRecord
from miniagent.contracts.transitions import transition_record
from miniagent.task_store import StoreError, TaskStore


def at(second: int = 0) -> str:
    return f"2026-11-17T00:00:{second:02d}Z"


def task(identity="root", **changes) -> TaskRecord:
    data = dict(task_id=identity, root_id=identity, parent_id=None, creation_key=f"create:{identity}",
                objective=f"Maintain {identity}", project_id="project", owner_profile_id="owner",
                assignee_profile_id="worker", deps=[], constraints_ref={"id": f"constraints:{identity}", "version": 1},
                acceptance_ref=None, budget_scope_id=f"budget:{identity}", input_artifact_refs=[], output_artifact_refs=[],
                status="created", reason=None, current_attempt_id=None, latest_report_id=None, wait_ref=None,
                created_at=at(), updated_at=at(), revision=1)
    data.update(changes)
    return TaskRecord(**data)


def attempt(task_id="root", identity="a1", **changes) -> AttemptRecord:
    data = dict(attempt_id=identity, task_id=task_id, number=1, state="running", context_ref=f"context:{identity}",
                checkpoint_id=None, report_ids=[], started_at=at(1), ended_at=None, end_reason=None,
                created_at=at(1), updated_at=at(1), revision=2)
    data.update(changes)
    return AttemptRecord(**data)


def run(task_id="root", attempt_id="a1", identity="run1", **changes) -> RunRecord:
    data = dict(run_id=identity, task_id=task_id, attempt_id=attempt_id, state="running", started_at=at(1),
                ended_at=None, end_reason=None, resumed_from=None, created_at=at(1), updated_at=at(1), revision=2)
    data.update(changes)
    return RunRecord(**data)


def spec(identity="root", version=1, **changes) -> AcceptanceSpec:
    data = dict(spec_id=f"spec:{identity}", version=version, task_id=identity, objective=f"Maintain {identity}",
                target={"project_id": "runtime-local:test", "environment_id": "sandbox", "resource_ids": []},
                criteria=[{"id": "check", "description": "Host check", "required": True}],
                constraints_ref={"id": f"constraints:{identity}", "version": 1},
                evaluator_ref={"id": "checker", "version": 1, "kind": "deterministic"}, params={},
                required_output_roles=[], authority_ref="trusted-host", created_at=at(2))
    data.update(changes)
    return AcceptanceSpec(**data)


def report(identity="root", attempt_id="a1", version=1, **changes) -> AcceptanceReport:
    evidence = [{"kind": "receipt", "id": "receipt:test"}]
    data = dict(report_id=f"report:{identity}", task_id=identity, attempt_id=attempt_id,
                spec_ref={"id": f"spec:{identity}", "version": version},
                evaluator_ref={"id": "checker", "version": 1, "kind": "deterministic"}, verdict="passed",
                checks=[{"criterion_id": "check", "verdict": "passed", "reason": "Recorded host assertion", "evidence_refs": evidence}],
                evaluated_artifact_refs=[], evaluated_resource_versions=[], evidence_refs=evidence,
                reason=None, evaluated_at=at(3), created_at=at(3))
    data.update(changes)
    return AcceptanceReport(**data)


def checkpoint(identity="cp1", **changes) -> Checkpoint:
    data = dict(checkpoint_id=identity, task_id="root", attempt_id="a1", run_id="run1",
                context_ref="context:a1", history_ref="history:cp1", created_at=at(2),
                resume_cursor={"step": 1, "phase": "before_model", "pending_action_ids": [],
                               "pending_tool_call_ids": [], "observed_tool_call_ids": []},
                constraints_ref={"id": "constraints:root", "version": 1}, budget_scope_id="budget:root",
                acceptance_failures=0, loop_state={}, applied_message_ids=[], wait_ref=None,
                pending_delegations=[], completed_action_ids=[])
    data.update(changes)
    return Checkpoint(**data)


def _race_cas(path: str, ready, release, outcomes) -> None:
    try:
        store = TaskStore(path)
        original = store.get("root")
        ready.put(True)
        if not release.wait(10):
            raise RuntimeError("race release timed out")
        updated = transition_record(original, "blocked", expected_revision=original.revision,
                                    at=at(1), reason="claim test")
        with store.transaction() as tx:
            tx.update(updated, expected_revision=original.revision)
            tx.emit("claimed", "root", {"revision": updated.revision})
        outcomes.put("ok")
    except StoreError as exc:
        outcomes.put(exc.code)
    except Exception as exc:
        outcomes.put(type(exc).__name__)


def _crash_transaction(path: str, ready) -> None:
    store = TaskStore(path)
    with store.transaction() as tx:
        original = tx.get("TaskRecord", "root")
        tx.update(transition_record(original, "blocked", expected_revision=1, at=at(1), reason="not committed"),
                  expected_revision=1)
        tx.put_blob("uncommitted", "context", {"private": "not committed"})
        tx.emit("uncommitted", "root", {})
        ready.put(True)
        ready.close()
        ready.join_thread()
        os._exit(23)  # Bypass context managers/finally to model a dead worker.


class TaskStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="miniagent-store-test-")
        self.path = Path(self.temporary.name) / "tasks.sqlite3"
        self.store = TaskStore(self.path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assert_error(self, code, function) -> None:
        with self.assertRaises(StoreError) as raised:
            function()
        self.assertEqual(raised.exception.code, code)

    def running(self) -> None:
        with self.store.transaction() as tx:
            tx.insert(task(status="running", current_attempt_id="a1", revision=3, updated_at=at(1)))
            tx.put_blob("context:a1", "task_context", {"task_id": "root"})
            tx.insert(attempt())
            tx.insert(run())

    def test_reopen_persists_records_bindings_blobs_and_pending_outbox(self) -> None:
        original = task()
        with self.store.transaction() as tx:
            tx.insert(original)
            tx.bind_creation(original.creation_key, "signature", "root")
            tx.put_blob("context", "task_context", {"nested": [1, 1.25, True, None]})
            event = tx.emit("task_created", "root", {"task_id": "root"})
        reopened = TaskStore(self.path)
        self.assertEqual(reopened.get("root"), original)
        self.assertEqual(reopened.get_blob("context"), {"nested": [1, 1.25, True, None]})
        with reopened.transaction() as tx:
            self.assertEqual(tx.creation(original.creation_key), {"signature": "signature", "task_id": "root"})
        self.assertEqual(reopened.pending_events(), [event])
        with closing(sqlite3.connect(self.path)) as connection, connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_exception_rolls_back_state_binding_blob_and_outbox_together(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "cancel transaction"):
            with self.store.transaction() as tx:
                tx.insert(task())
                tx.bind_creation("create:root", "signature", "root")
                tx.put_blob("context", "context", {})
                tx.emit("task_created", "root", {})
                raise RuntimeError("cancel transaction")
        self.assert_error("not_found", lambda: self.store.get("root"))
        self.assert_error("not_found", lambda: self.store.get_blob("context"))
        self.assertEqual(self.store.events(), [])
        with self.store.transaction() as tx:
            self.assertIsNone(tx.creation("create:root"))

    def test_commit_integrity_failure_rolls_back_all_writes(self) -> None:
        with self.assertRaises(StoreError):
            with self.store.transaction() as tx:
                tx.insert(task(deps=["missing"]))
                tx.put_blob("context", "context", {})
                tx.emit("task_created", "root", {})
        self.assertEqual(self.store.tasks(), [])
        self.assertEqual(self.store.events(), [])
        self.assert_error("not_found", lambda: self.store.get_blob("context"))

    def test_insert_and_creation_binding_are_idempotent_but_conflicts_fail(self) -> None:
        with self.store.transaction() as tx:
            tx.insert(task())
            tx.insert(task())
            tx.bind_creation("create:root", "signature", "root")
            tx.bind_creation("create:root", "signature", "root")
        for mutation in [lambda tx: tx.insert(task(objective="different")),
                         lambda tx: tx.bind_creation("create:root", "different", "root"),
                         lambda tx: tx.bind_creation("different", "signature", "root")]:
            with self.subTest(mutation=mutation), self.assertRaises(StoreError) as raised:
                with self.store.transaction() as tx:
                    mutation(tx)
            self.assertEqual(raised.exception.code, "conflict")
        self.assertEqual(self.store.get("root").objective, "Maintain root")

    def test_creation_binding_must_match_stored_task_key(self) -> None:
        with self.assertRaises(StoreError):
            with self.store.transaction() as tx:
                tx.bind_creation("wrong-key", "signature", "root")
                tx.insert(task())
        self.assertEqual(self.store.tasks(), [])

    def test_cas_and_transition_checks_reject_stale_and_terminal_updates(self) -> None:
        original = task()
        with self.store.transaction() as tx:
            tx.insert(original)
        blocked = transition_record(original, "blocked", expected_revision=1, at=at(1), reason="wait")
        with self.store.transaction() as tx:
            tx.update(blocked, expected_revision=1)
        with self.assertRaises(StoreError) as raised:
            with self.store.transaction() as tx:
                tx.update(blocked, expected_revision=1)
        self.assertEqual(raised.exception.code, "revision_conflict")
        cancelled = transition_record(blocked, "cancelled", expected_revision=2, at=at(2), reason="cancelled")
        with self.store.transaction() as tx:
            tx.update(cancelled, expected_revision=2)
        invalid = cancelled.model_copy(update={"revision": 4, "updated_at": at(3), "reason": "changed"})
        with self.assertRaises(StoreError):
            with self.store.transaction() as tx:
                tx.update(invalid, expected_revision=3)
        self.assertEqual(self.store.get("root"), cancelled)

    def test_unchecked_invalid_models_cannot_bypass_contract_validation(self) -> None:
        for invalid in [task().model_copy(update={"revision": True}),
                        task().model_copy(update={"status": "running"}),
                        task().model_copy(update={"schema_version": 2})]:
            with self.subTest(invalid=invalid), self.assertRaises(StoreError):
                with self.store.transaction() as tx:
                    tx.insert(invalid)
        self.assertEqual(self.store.tasks(), [])

    def test_multiple_coupled_state_transitions_validate_only_at_commit(self) -> None:
        original = task()
        with self.store.transaction() as tx:
            tx.insert(original)
        with self.store.transaction() as tx:
            ready = transition_record(original, "ready", expected_revision=1, at=at(1))
            tx.update(ready, expected_revision=1)
            running = transition_record(ready, "running", expected_revision=2, at=at(1), current_attempt_id="a1")
            tx.update(running, expected_revision=2)
            tx.insert(run())
            tx.insert(attempt())
            tx.put_blob("context:a1", "task_context", {})
            tx.emit("started", "root", {"attempt_id": "a1", "run_id": "run1"})
        self.assertEqual(self.store.get("root").status, "running")
        self.assertEqual(self.store.attempts("root")[0].attempt_id, "a1")
        self.assertEqual(self.store.runs("a1")[0].run_id, "run1")

    def test_dependency_and_combined_parent_wait_cycles_rejected(self) -> None:
        cases = [
            [task(), task("child", root_id="root", parent_id="root", deps=["root"])],
            [task(), task("a", root_id="root", parent_id="root", deps=["b"]),
             task("b", root_id="root", parent_id="root", deps=["a"])],
            [task(), task("a", root_id="root", parent_id="b"), task("b", root_id="root", parent_id="a")],
        ]
        for records in cases:
            with self.subTest(records=records), self.assertRaises(StoreError):
                with self.store.transaction() as tx:
                    for record in records:
                        tx.insert(record)
            self.assertEqual(self.store.tasks(), [])

    def test_graph_registration_is_atomic_and_read_filters_are_logical(self) -> None:
        with self.store.transaction() as tx:
            tx.insert(task("a", root_id="root", parent_id="root", deps=["b"]))
            tx.insert(task("b", root_id="root", parent_id="root"))
            tx.insert(task())
            tx.insert(task("other"))
        self.assertEqual({item.task_id for item in self.store.tasks("root")}, {"root", "a", "b"})
        self.assertEqual({item.task_id for item in self.store.children("root")}, {"a", "b"})
        with self.assertRaises(StoreError):
            with self.store.transaction() as tx:
                original = tx.get("TaskRecord", "a")
                tx.update(transition_record(original, "ready", expected_revision=1, at=at(1)), expected_revision=1)

    def test_cross_root_and_project_references_rejected(self) -> None:
        for child in [task("child", root_id="root", parent_id="root", project_id="other"),
                      task("child", root_id="root", parent_id="root", deps=["other"])]:
            with self.subTest(child=child), self.assertRaises(StoreError):
                with self.store.transaction() as tx:
                    for record in (task(), task("other"), child):
                        tx.insert(record)

    def test_running_attempt_needs_exactly_one_running_run_and_context(self) -> None:
        for missing in ["run", "context"]:
            with self.subTest(missing=missing), self.assertRaises(StoreError):
                with self.store.transaction() as tx:
                    tx.insert(task(status="running", current_attempt_id="a1", updated_at=at(1)))
                    tx.insert(attempt())
                    if missing != "context":
                        tx.put_blob("context:a1", "context", {})
                    if missing != "run":
                        tx.insert(run())

    def test_multiple_open_runs_or_attempts_rejected(self) -> None:
        self.running()
        with self.assertRaises(StoreError):
            with self.store.transaction() as tx:
                tx.insert(run(identity="run2"))
        with self.assertRaises(StoreError):
            with self.store.transaction() as tx:
                tx.put_blob("context:a2", "context", {})
                tx.insert(attempt(identity="a2", number=2))
                tx.insert(run(attempt_id="a2", identity="run2"))
        self.assertEqual(len(self.store.runs("a1")), 1)
        self.assertEqual(len(self.store.attempts("root")), 1)

    def test_run_cannot_reference_another_tasks_attempt(self) -> None:
        with self.assertRaises(StoreError):
            with self.store.transaction() as tx:
                tx.insert(task())
                tx.insert(task("other", status="running", current_attempt_id="a1", updated_at=at(1)))
                tx.put_blob("context:a1", "context", {})
                tx.insert(attempt(task_id="other"))
                tx.insert(run(task_id="root"))

    def test_historical_run_overlap_rejected(self) -> None:
        with self.assertRaises(StoreError):
            with self.store.transaction() as tx:
                tx.insert(task(status="failed", current_attempt_id="a1", reason="failure", updated_at=at(9)))
                tx.put_blob("context:a1", "context", {})
                tx.insert(attempt(state="failed", ended_at=at(9), end_reason="failure", updated_at=at(9)))
                tx.insert(run(state="ended", ended_at=at(6), end_reason="done", updated_at=at(6)))
                tx.insert(run(identity="run2", state="ended", started_at=at(5), ended_at=at(8), end_reason="done", updated_at=at(8)))

    def test_parent_cannot_close_before_open_or_later_closed_descendants(self) -> None:
        for child in [task("child", root_id="root", parent_id="root"),
                      task("child", root_id="root", parent_id="root", status="cancelled", reason="cancelled", updated_at=at(5))]:
            with self.subTest(child=child), self.assertRaises(StoreError):
                with self.store.transaction() as tx:
                    tx.insert(task(status="cancelled", reason="cancelled", updated_at=at(3)))
                    tx.insert(child)

    def test_exact_spec_versions_and_terminal_report_bindings(self) -> None:
        self.running()
        with self.store.transaction() as tx:
            tx.insert(spec(version=1))
            tx.insert(spec(version=2))
            tx.insert(report())
            original = tx.get("TaskRecord", "root")
            verifying = transition_record(original, "verifying", expected_revision=3, at=at(4))
            tx.update(verifying, expected_revision=3)
            finished = transition_record(verifying, "completed", expected_revision=4, at=at(5),
                acceptance_ref={"id": "spec:root", "version": 1}, latest_report_id="report:root")
            tx.update(finished, expected_revision=4)
            tx.update(transition_record(tx.get("AttemptRecord", "a1"), "succeeded", expected_revision=2,
                      at=at(5), report_ids=["report:root"], ended_at=at(5), end_reason="completed"), expected_revision=2)
            tx.update(transition_record(tx.get("RunRecord", "run1"), "ended", expected_revision=2,
                      at=at(5), ended_at=at(5), end_reason="completed"), expected_revision=2)
        self.assertEqual(self.store.get_record("AcceptanceSpec", "spec:root").version, 2)
        self.assertEqual(self.store.get_record("AcceptanceSpec", ("spec:root", 1)).version, 1)
        self.assertEqual(self.store.get("root").acceptance_ref.version, 1)
        self.assertEqual(self.store.get("root").status, "completed")
        # Store checks recorded assertions; receipt authenticity/closure belongs
        # to TaskService, so this structural fixture does not assert verification.
        with self.assertRaises(StoreError):
            with self.store.transaction() as tx:
                tx.insert(spec(version=1, objective="changed immutable requirement"))

    def test_report_binding_requires_exact_existing_spec_and_same_attempt_task(self) -> None:
        self.running()
        for wrong_report in [report(version=9), report(attempt_id="missing")]:
            with self.subTest(report=wrong_report), self.assertRaises(StoreError):
                with self.store.transaction() as tx:
                    tx.insert(spec())
                    tx.insert(wrong_report)
        with self.assertRaises(StoreError):
            with self.store.transaction() as tx:
                tx.insert(task("other"))
                tx.insert(spec(identity="other"))
                tx.insert(report(identity="other", attempt_id="a1"))

    def test_checkpoints_persist_without_claiming_executable_recovery(self) -> None:
        self.running()
        saved = checkpoint()
        with self.store.transaction() as tx:
            tx.put_blob("history:cp1", "history", [{"role": "user", "content": "maintain"}])
            tx.insert(saved)
        self.assertEqual(TaskStore(self.path).get_record("Checkpoint", "cp1"), saved)
        self.assertEqual(self.store.get("root").status, "running")
        self.assertEqual(self.store.runs("a1")[0].state, "running")

    def test_checkpoint_causal_and_context_references_are_checked(self) -> None:
        self.running()
        for value in [checkpoint(history_ref="missing"), checkpoint(run_id="missing"),
                      checkpoint(created_at=at()), checkpoint(budget_scope_id="other")]:
            with self.subTest(checkpoint=value), self.assertRaises(StoreError):
                with self.store.transaction() as tx:
                    tx.put_blob("history:cp1", "history", [])
                    tx.insert(value)

    def test_blobs_are_immutable_json_copies(self) -> None:
        with self.store.transaction() as tx:
            returned = tx.put_blob("blob", "kind", {"items": [1]})
            returned["items"].append(2)
            self.assertEqual(tx.put_blob("blob", "kind", {"items": [1]}), {"items": [1]})
        received = self.store.get_blob("blob")
        received["items"].append(3)
        self.assertEqual(self.store.get_blob("blob"), {"items": [1]})
        for kind, payload in [("kind", {"items": [2]}), ("other", {"items": [1]})]:
            with self.subTest(kind=kind), self.assertRaises(StoreError) as raised:
                with self.store.transaction() as tx:
                    tx.put_blob("blob", kind, payload)
            self.assertEqual(raised.exception.code, "conflict")
        for payload in [{"n": float("nan")}, {1: "nonstring key"}, {"object": object()}]:
            with self.subTest(payload=payload), self.assertRaises(StoreError):
                with self.store.transaction() as tx:
                    tx.put_blob("invalid", "kind", payload)

    def test_outbox_delivery_is_replayable_with_idempotent_acknowledgement(self) -> None:
        with self.store.transaction() as tx:
            tx.insert(task())
            tx.insert(task("other"))
            first = tx.emit("created", "root", {"n": 1})
            second = tx.emit("created", "other", {"n": 2})
        self.assertLess(first["sequence"], second["sequence"])
        self.assertNotEqual(first["event_id"], second["event_id"])
        self.assertEqual(self.store.pending_events(), self.store.pending_events())
        self.assertEqual(self.store.events(after_sequence=first["sequence"]), [second])
        self.assertEqual(self.store.events(task_id="root"), [first])
        self.store.acknowledge(first["event_id"])
        delivered = self.store.events()[0]
        self.store.acknowledge(first["event_id"])
        self.assertEqual(self.store.events()[0], delivered)
        self.assertEqual(self.store.pending_events(), [second])
        self.assert_error("not_found", lambda: self.store.acknowledge("missing"))

    def test_outbox_cannot_point_to_missing_task(self) -> None:
        with self.assertRaises(StoreError):
            with self.store.transaction() as tx:
                tx.emit("created", "missing", {})
        self.assertEqual(self.store.events(), [])

    def test_closed_transaction_and_unknown_schema_are_rejected(self) -> None:
        with self.store.transaction() as tx:
            tx.insert(task())
        self.assert_error("integrity_error", lambda: tx.get("TaskRecord", "root"))
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("PRAGMA user_version=2")
        self.assert_error("schema_version", lambda: TaskStore(self.path))
        self.assert_error("schema_version", lambda: self.store.get("root"))

    def test_initialization_rejects_foreign_or_incompatible_databases(self) -> None:
        foreign = Path(self.temporary.name) / "foreign.sqlite3"
        with closing(sqlite3.connect(foreign)) as connection, connection:
            connection.execute("CREATE TABLE unrelated (value TEXT)")
            connection.execute("INSERT INTO unrelated VALUES ('keep')")
        self.assert_error("schema_version", lambda: TaskStore(foreign))
        with closing(sqlite3.connect(foreign)) as connection, connection:
            self.assertEqual(connection.execute("SELECT value FROM unrelated").fetchone()[0], "keep")
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [("unrelated",)])
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("DROP TABLE blobs")
        self.assert_error("schema_version", lambda: TaskStore(self.path))
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("CREATE TABLE blobs (blob_id TEXT PRIMARY KEY, payload TEXT)")
        self.assert_error("schema_version", lambda: TaskStore(self.path))

    @unittest.skipUnless(os.name == "posix", "POSIX file permission check")
    def test_new_file_is_private_and_existing_permissions_are_unchanged(self) -> None:
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        os.chmod(self.path, 0o640)
        TaskStore(self.path)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)

    def test_process_exit_mid_transaction_leaves_no_partial_state_or_event(self) -> None:
        with self.store.transaction() as tx:
            tx.insert(task())
        context = multiprocessing.get_context("spawn")
        ready = context.Queue()
        process = context.Process(target=_crash_transaction, args=(str(self.path), ready))
        try:
            process.start()
            self.assertTrue(ready.get(timeout=15))
            process.join(timeout=15)
            self.assertEqual(process.exitcode, 23)
            reopened = TaskStore(self.path)
            self.assertEqual(reopened.get("root"), task())
            self.assertEqual(reopened.pending_events(), [])
            self.assert_error("not_found", lambda: reopened.get_blob("uncommitted"))
        finally:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
            ready.close()

    def test_two_process_compare_and_swap_commits_one_state_and_one_event(self) -> None:
        with self.store.transaction() as tx:
            tx.insert(task())
        context = multiprocessing.get_context("spawn")
        ready, outcomes, release = context.Queue(), context.Queue(), context.Event()
        processes = [context.Process(target=_race_cas, args=(str(self.path), ready, release, outcomes)) for _ in range(2)]
        try:
            for process in processes:
                process.start()
            for _ in processes:
                self.assertTrue(ready.get(timeout=15))
            release.set()
            self.assertEqual(sorted(outcomes.get(timeout=15) for _ in processes), ["ok", "revision_conflict"])
            for process in processes:
                process.join(timeout=15)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(self.store.get("root").revision, 2)
            self.assertEqual(len(self.store.events()), 1)
        finally:
            release.set()
            for process in processes:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=5)
            ready.close()
            outcomes.close()


if __name__ == "__main__":
    unittest.main()
