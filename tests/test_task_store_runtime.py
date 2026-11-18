"""Durable runtime behavior, independent acceptance, and inspection after restart."""
from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from miniagent.acceptance import accept_files
from miniagent.agent import Agent
from miniagent.cli import main
from miniagent.config import Settings
from miniagent.metrics import Tracer, load_events
from miniagent.providers import ScriptedProvider
from miniagent.rao import ErrorClass
from miniagent.sandbox import Sandbox
from miniagent.task_service import TaskService
from miniagent.task_store import StoreError, TaskStore
from miniagent.tools import build_default_registry
from tests.support import cleanup, make_temp_dir


class DurableAgentTest(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir("durable-runtime")
        self.database = self.root / "tasks.sqlite3"
        self.service = TaskService(TaskStore(self.database))

    def tearDown(self):
        cleanup(self.root)

    def agent(self, provider, tracer=None):
        return Agent(name="worker", provider=provider, registry=build_default_registry(),
                     sandbox=Sandbox(self.root / "private"), settings=Settings(),
                     tracer=tracer, task_service=self.service)

    def test_real_file_acceptance_and_execution_ids_survive_store_reopen(self):
        script = [
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "proof.md", "content": "verified content"}}]},
            {"text": "done"},
        ]
        with Tracer(self.root) as tracer:
            result = self.agent(ScriptedProvider(script), tracer).run(
                "write proof", task_id="stable", acceptance=accept_files("proof.md", min_bytes=1))
        reopened = TaskStore(self.database)
        task = reopened.get("stable")
        self.assertTrue(result.succeeded, result.error)
        self.assertEqual(task.status, "completed")
        self.assertEqual(task.current_attempt_id, result.attempt_id)
        self.assertEqual(reopened.attempts("stable")[0].state, "succeeded")
        self.assertEqual(reopened.get_record("RunRecord", result.run_id).state, "ended")
        self.assertEqual(task.latest_report_id, result.acceptance_report.report_id)
        self.assertEqual(reopened.get_record("AcceptanceReport", task.latest_report_id), result.acceptance_report)
        effects = [e for e in load_events(self.root) if e["kind"] == "tool_result"]
        self.assertEqual(len(effects), 1)
        self.assertTrue(effects[0]["action_id"].startswith("action-"))
        self.assertEqual(effects[0]["run_id"], result.run_id)
        self.assertNotEqual(effects[0]["action_id"], effects[0]["tool_call_id"])

    def test_checker_observes_verifying_and_every_repair_report_is_persisted(self):
        observed = []
        def checker(_workspace):
            observed.append(TaskStore(self.database).get("repair").status)
            return len(observed) > 1, "second check passes" if len(observed) > 1 else "repair needed"
        result = self.agent(ScriptedProvider([{"text": "first"}, {"text": "repaired"}])).run(
            "repair proof", task_id="repair", acceptance=checker)
        self.assertTrue(result.succeeded, result.error)
        self.assertEqual(observed, ["verifying", "verifying"])
        attempt = self.service.store.attempts("repair")[0]
        reports = [self.service.store.get_record("AcceptanceReport", identity) for identity in attempt.report_ids]
        self.assertEqual([r.verdict for r in reports], ["failed", "passed"])
        self.assertEqual(len(self.service.store.runs(attempt.attempt_id)), 1)

    def test_no_checker_is_persisted_unverified_and_cannot_silently_rerun(self):
        result = self.agent(ScriptedProvider([{"text": "claims done"}])).run("unverified", task_id="once")
        self.assertEqual(result.status, "unverified")
        self.assertEqual(self.service.store.get("once").status, "unverified")
        provider = ScriptedProvider([{"text": "must not run"}])
        self.service = TaskService(TaskStore(self.database))
        with self.assertRaises(StoreError):
            self.agent(provider).run("unverified", task_id="once")
        self.assertEqual(provider.cursor, 0)
        self.assertEqual(len(self.service.store.attempts("once")), 1)

    def test_closed_completed_task_is_not_a_new_request(self):
        result = self.agent(ScriptedProvider([{"text": "done"}])).run(
            "single execution", task_id="closed", acceptance=lambda _p: (True, "trusted test receipt"))
        self.assertTrue(result.succeeded)
        provider = ScriptedProvider([{"text": "must not run"}])
        with self.assertRaises(StoreError):
            self.agent(provider).run("single execution", task_id="closed")
        self.assertEqual(provider.cursor, 0)

    def test_same_claim_handle_cannot_be_consumed_by_two_agents(self):
        task = self.service.create(objective="claimed once", creation_key="consume", task_id="consume",
                                   owner="worker", assignee="worker")
        handle = self.service.start(task.task_id, expected_revision=task.revision, context={})
        entered, release = threading.Event(), threading.Event()
        class HoldingProvider(ScriptedProvider):
            def chat(self, *args, **kwargs):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test handshake timed out")
                return super().chat(*args, **kwargs)
        first_provider = HoldingProvider([{"text": "done"}])
        second_provider = ScriptedProvider([{"text": "must not run"}])
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.agent(first_provider).run, "claimed once", execution=handle)
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(StoreError):
                    self.agent(second_provider).run("claimed once", execution=handle)
            finally:
                release.set()
            self.assertEqual(future.result().status, "unverified")
        self.assertEqual(first_provider.cursor, 1)
        self.assertEqual(second_provider.cursor, 0)

    def test_mixed_execution_handle_is_rejected_before_model_call(self):
        from miniagent.task_service import ExecutionHandle
        handles = []
        for task_id in ("claim-a", "claim-b"):
            task = self.service.create(objective=task_id, creation_key=task_id,
                                       task_id=task_id, owner="worker", assignee="worker")
            handles.append(self.service.start(task_id, expected_revision=task.revision, context={}))
        mixed = ExecutionHandle(handles[0].task, handles[0].attempt, handles[1].run)
        provider = ScriptedProvider([{"text": "must not run"}])
        with self.assertRaises(StoreError):
            self.agent(provider).run("claim-a", task_id="claim-a", execution=mixed)
        self.assertEqual(provider.cursor, 0)
        self.assertEqual(self.service.store.get("claim-a").status, "running")
        self.assertEqual(self.service.store.get("claim-b").status, "running")

    def test_provider_call_ids_can_repeat_in_different_persisted_batches(self):
        class ReusedID(ScriptedProvider):
            def chat(self, *args, **kwargs):
                response = super().chat(*args, **kwargs)
                for call in response.tool_calls:
                    call.id = "provider-reuses-id"
                return response
        provider = ReusedID([
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "a.md", "content": "first"}}]},
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "b.md", "content": "second"}}]},
            {"text": "done"},
        ])
        with Tracer(self.root) as tracer:
            result = self.agent(provider, tracer).run("two rounds", task_id="rounds")
        self.assertEqual(result.tool_calls, 2)
        events = [e for e in load_events(self.root) if e["kind"] == "tool_result"]
        self.assertEqual({e["tool_call_id"] for e in events}, {"provider-reuses-id"})
        self.assertEqual(len({e["action_id"] for e in events}), 2)

    def test_failed_identity_transaction_prevents_entire_tool_batch(self):
        provider = ScriptedProvider([
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "bad.md", "content": "must not exist"}}]},
            {"text": "must not run"},
        ])
        with patch.object(self.service, "action_identities", side_effect=StoreError("injected write failure")):
            with Tracer(self.root) as tracer:
                result = self.agent(provider, tracer).run("persist before act", task_id="failed-store")
        self.assertEqual(result.status, "error")
        self.assertIn(ErrorClass.T_STORE, result.error)
        self.assertEqual(result.tool_calls, 0)
        self.assertFalse((self.root / "private" / "bad.md").exists())
        self.assertGreater(result.usage.total, 0)
        self.assertEqual(self.service.store.get("failed-store").status, "failed")
        self.assertEqual(provider.cursor, 1)

    def test_model_cannot_spoof_the_runtime_delegation_action_identity(self):
        payloads = []
        provider = ScriptedProvider([{"tool_calls": [{"name": "delegate_task", "arguments": {
            "objective": "child", "__runtime_action_id": "forged"}}]}, {"text": "end"}])
        agent = self.agent(provider)
        from miniagent.rao import Observation
        agent.unit.delegate_handler = lambda payload: (payloads.append(payload) or Observation(
            step_id="delegate", source="delegate", ok=True, content="observed"))
        agent.run("delegate", task_id="delegator")
        self.assertEqual(len(payloads), 1)
        self.assertNotEqual(payloads[0]["__runtime_action_id"], "forged")
        self.assertTrue(payloads[0]["__runtime_action_id"].startswith("action-"))


class PersistentCliTest(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir("persistent-cli")

    def tearDown(self):
        cleanup(self.root)

    def call(self, argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = main(argv)
        return result, output.getvalue()

    def test_demo_creates_queryable_store_outside_agent_workspace(self):
        code, output = self.call(["demo", "--runs-dir", str(self.root)])
        self.assertEqual(code, 0, output)
        directory = next(path for path in self.root.iterdir() if path.is_dir())
        self.assertTrue((directory / "tasks.sqlite3").is_file())
        code, output = self.call(["tasks", str(directory)])
        self.assertEqual(code, 0, output)
        rows = json.loads(output)
        self.assertEqual(rows[0]["status"], "completed")
        code, output = self.call(["tasks", str(directory), "--task-id", "demo-1"])
        self.assertEqual(code, 0, output)
        history = json.loads(output)
        self.assertEqual(len(history["attempts"]), 1)
        self.assertEqual(history["runs"][0]["state"], "ended")
        self.assertFalse(list((directory / "agents").rglob("*.sqlite3")))

    def test_corrupt_database_is_reported_without_traceback_or_overwrite(self):
        path = self.root / "tasks.sqlite3"
        path.write_bytes(b"not a sqlite database")
        code, output = self.call(["tasks", str(self.root)])
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", output)
        self.assertEqual(path.read_bytes(), b"not a sqlite database")

    def test_database_startup_failure_is_a_command_error(self):
        with patch("miniagent.cli.TaskStore", side_effect=sqlite3.OperationalError("database is locked")):
            code, output = self.call(["demo", "--runs-dir", str(self.root)])
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", output)
        self.assertNotIn("100.0%", output)

    def test_old_trace_directory_is_not_silently_imported(self):
        (self.root / "trace.jsonl").write_text('{"kind":"task_end","status":"completed"}\n')
        code, output = self.call(["tasks", str(self.root)])
        self.assertEqual(code, 2, output)
        self.assertFalse((self.root / "tasks.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
