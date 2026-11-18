"""The browser service must execute the real runtime and report durable facts."""
from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.acceptance_evidence import validate_evidence
from miniagent.contracts.acceptance import AcceptanceReport, AcceptanceSpec
from miniagent.providers import ScriptedProvider
from miniagent.studio_service import StudioError, StudioService
from miniagent.task_service import TaskService
from miniagent.task_store import TaskStore
from tests.support import cleanup, make_temp_dir


def request(mode="demo", **changes):
    return {"request_id": str(uuid.uuid4()), "mode": mode, **changes}


def finished(service, run_id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        value = service.detail(run_id)
        if value["run"]["status"] not in {"queued", "running", "created", "ready", "verifying"}:
            return value
        time.sleep(0.01)
    raise AssertionError("studio execution did not finish")


class StudioServiceTest(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir("studio-service")
        self.service = StudioService(self.root / "runs", Settings())

    def tearDown(self):
        self.service.close()
        cleanup(self.root)

    def demo(self):
        run = self.service.start(request())
        detail = finished(self.service, run["id"])
        self.assertEqual(detail["run"]["status"], "completed", detail["run"])
        return run["id"], detail

    def test_demo_uses_real_tools_acceptance_and_persisted_execution(self):
        run_id, detail = self.demo()
        self.assertEqual(len(detail["tasks"]), 1)
        task = detail["tasks"][0]
        store = TaskStore(self.root / "runs" / run_id / "tasks.sqlite3")
        self.assertEqual(store.get(task["task_id"]).status, "completed")
        self.assertEqual(store.attempts(task["task_id"])[0].state, "succeeded")
        self.assertEqual(store.runs(task["current_attempt_id"])[0].state, "ended")
        artifact = next(a for a in detail["artifacts"] if a["name"] == "notes.md")
        self.assertIn("演示产物", self.service.artifact(run_id, artifact["path"])["content"])
        reports = detail["acceptance"]
        self.assertEqual(reports[0]["report"]["verdict"], "passed")
        self.assertTrue(reports[0]["receipts"])
        self.assertEqual(reports[0]["report"]["report_id"], task["latest_report_id"])
        spec = AcceptanceSpec.model_validate(reports[0]["spec"])
        report = AcceptanceReport.model_validate(reports[0]["report"])
        self.assertEqual((spec.spec_id, spec.version), (report.spec_ref.id, report.spec_ref.version))
        validate_evidence(spec, report, reports[0]["receipts"])
        self.assertIn("100.0%", detail["report"])
        tools = [event for event in detail["events"] if event["kind"] == "tool_result"]
        self.assertTrue(tools)
        self.assertTrue(all(event["action_id"] and event["run_id"] for event in tools))

    def test_team_demo_persists_dependency_tree_and_independent_root_report(self):
        run = self.service.start(request("team-demo"))
        detail = finished(self.service, run["id"])
        self.assertEqual(detail["run"]["status"], "completed", detail["run"])
        self.assertEqual(len(detail["tasks"]), 3)
        roots = [task for task in detail["tasks"] if task["parent_id"] is None]
        children = [task for task in detail["tasks"] if task["parent_id"] is not None]
        self.assertEqual(len(roots), 1)
        self.assertEqual({task["parent_id"] for task in children}, {roots[0]["task_id"]})
        self.assertEqual({task["status"] for task in detail["tasks"]}, {"completed"})
        self.assertEqual(sum(len(task["deps"]) for task in children), 1)
        self.assertEqual({a["name"] for a in detail["artifacts"]}, {"notes.md", "summary.md"})
        report_tasks = {row["task_id"] for row in detail["acceptance"] if row["report"]["verdict"] == "passed"}
        self.assertEqual(report_tasks, {task["task_id"] for task in detail["tasks"]})
        for row in detail["acceptance"]:
            validate_evidence(AcceptanceSpec.model_validate(row["spec"]),
                              AcceptanceReport.model_validate(row["report"]), row["receipts"])

    def test_request_replay_after_reopen_does_not_execute_again(self):
        payload = request()
        run = self.service.start(payload)
        before = finished(self.service, run["id"])
        self.service.close()
        self.service = StudioService(self.root / "runs", Settings())
        with patch.object(self.service, "_execute", side_effect=AssertionError("must not execute")):
            repeated = self.service.start(payload)
            after = self.service.detail(run["id"])
        self.assertEqual(repeated["id"], run["id"])
        self.assertEqual(before["tasks"], after["tasks"])
        self.assertEqual(before["events"], after["events"])
        self.assertEqual(len(self.service.list_runs()["runs"]), 1)

    def test_conflicting_replay_cannot_replace_original_task(self):
        payload = request()
        run = self.service.start(payload)
        finished(self.service, run["id"])
        with self.assertRaises(StudioError) as error:
            self.service.start({**payload, "mode": "team-demo"})
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.service.detail(run["id"])["run"]["mode"], "demo")

    def test_bounded_background_workers_return_without_waiting_and_replay_once(self):
        started, release = threading.Event(), threading.Event()
        calls = []
        original = self.service._execute
        def hold(run_id, payload):
            calls.append(run_id)
            started.set()
            if not release.wait(5):
                raise RuntimeError("test release timed out")
            return original(run_id, payload)
        with patch.object(self.service, "_execute", side_effect=hold):
            try:
                payload = request()
                first = self.service.start(payload)
                self.assertTrue(started.wait(2))
                repeated = self.service.start(payload)
                self.assertEqual(repeated["id"], first["id"])
                second = self.service.start(request())
                with self.assertRaises(StudioError) as error:
                    self.service.start(request())
                self.assertIn(error.exception.status, {409, 429, 503})
                self.assertEqual(len(self.service.list_runs()["runs"]), 2)
            finally:
                release.set()
            finished(self.service, first["id"])
            finished(self.service, second["id"])
        self.assertEqual(len(calls), 2)

    def test_missing_provider_configuration_rejects_before_dispatch(self):
        with self.assertRaises(StudioError):
            self.service.start(request("single", objective="write a report"))
        self.assertEqual(self.service.list_runs()["runs"], [])

    def test_custom_task_passes_options_and_user_file_checks_to_existing_runtime(self):
        configured = StudioService(self.root / "custom", Settings(api_key="synthetic-test-key"))
        provider = ScriptedProvider([
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "diagnosis.md", "content": "# Diagnosis\n\nVerified local output."}}]},
            {"text": "created diagnosis"},
        ])
        try:
            with patch("miniagent.studio_service.DeepSeekProvider", autospec=True, return_value=provider) as factory:
                run = configured.start(request("single", objective="Write diagnosis.md", thinking="enabled",
                                               acceptance_files=["diagnosis.md"]))
                detail = finished(configured, run["id"])
            self.assertEqual(detail["run"]["status"], "completed", detail["run"])
            self.assertEqual(factory.call_args.kwargs["thinking"], "enabled")
            self.assertEqual(detail["acceptance"][0]["report"]["verdict"], "passed")
            self.assertEqual(detail["artifacts"][0]["name"], "diagnosis.md")
        finally:
            configured.close()

    def test_custom_answer_without_checker_is_unverified_not_success(self):
        configured = StudioService(self.root / "unchecked", Settings(api_key="synthetic-test-key"))
        try:
            with patch("miniagent.studio_service.DeepSeekProvider", autospec=True,
                       return_value=ScriptedProvider([{"text": "I claim success"}])):
                run = configured.start(request("single", objective="answer a question"))
                detail = finished(configured, run["id"])
            self.assertEqual(detail["run"]["status"], "unverified")
            self.assertEqual(detail["tasks"][0]["status"], "unverified")
            self.assertEqual(detail["run"]["completed_count"], 0)
        finally:
            configured.close()

    def test_historical_real_request_replay_needs_no_provider_key(self):
        payload = request("single", objective="historical task")
        directory = self.root / "historical"
        configured = StudioService(directory, Settings(api_key="synthetic-test-key"))
        try:
            with patch("miniagent.studio_service.DeepSeekProvider", autospec=True,
                       return_value=ScriptedProvider([{"text": "historical answer"}])):
                run = configured.start(payload)
                before = finished(configured, run["id"])
        finally:
            configured.close()
        reopened = StudioService(directory, Settings())
        try:
            with patch.object(reopened, "_execute", side_effect=AssertionError("must not rerun")):
                repeated = reopened.start(payload)
            self.assertEqual(repeated["id"], run["id"])
            self.assertEqual(reopened.detail(run["id"])["tasks"], before["tasks"])
        finally:
            reopened.close()

    def test_invalid_requests_never_create_runs(self):
        for payload in [None, [], {}, request("not-a-mode"), request(request_id="../outside"),
                        request(mode=[]), request(scheduler={}), request(thinking=[]),
                        request("team-demo", scheduler="parallel"),
                        request(acceptance_files=["../outside.md"]),
                        request(acceptance_files=[".env"]),
                        request(thinking="sometimes")]:
            with self.subTest(payload=payload), self.assertRaises(StudioError):
                self.service.start(payload)
        self.assertEqual(self.service.list_runs()["runs"], [])

    def test_worker_error_is_visible_and_does_not_echo_exception_details(self):
        with patch.object(self.service, "_execute", side_effect=RuntimeError("synthetic-secret-api-key")):
            run = self.service.start(request())
            detail = finished(self.service, run["id"])
        self.assertEqual(detail["run"]["status"], "failed")
        self.assertNotIn("synthetic-secret-api-key", json.dumps(detail))
        self.assertEqual(self.service.list_runs()["active_count"], 0)

    def test_existing_cli_database_is_read_without_new_attempts(self):
        run_dir = self.root / "runs" / "existing-cli-run"
        service = TaskService(TaskStore(run_dir / "tasks.sqlite3"))
        task = service.create(task_id="cli-task", creation_key="cli-task", objective="existing CLI task",
                              owner="worker", assignee="worker")
        handle = service.start(task.task_id, expected_revision=task.revision, context={})
        detail = self.service.detail(run_dir.name)
        self.assertEqual(detail["tasks"][0]["status"], "running")
        self.assertNotEqual(detail["run"]["status"], "completed")
        self.assertEqual(service.store.get(task.task_id), handle.task)
        self.assertEqual(len(service.store.attempts(task.task_id)), 1)

    def test_corrupt_database_is_not_replaced_or_reported_successful(self):
        run_dir = self.root / "runs" / "broken-cli-run"
        run_dir.mkdir(parents=True)
        database = run_dir / "tasks.sqlite3"
        original = b"not a sqlite database"
        database.write_bytes(original)
        with self.assertRaises(StudioError):
            self.service.detail(run_dir.name)
        self.assertEqual(database.read_bytes(), original)
        self.assertNotIn("completed", {r["status"] for r in self.service.list_runs()["runs"]})

    def test_passed_history_requires_exact_spec_and_evidence_closure(self):
        for kind in ("spec", "receipts", "latest_report"):
            with self.subTest(missing=kind):
                run_id, detail = self.demo()
                report = detail["acceptance"][0]["report"]
                database = self.root / "runs" / run_id / "tasks.sqlite3"
                with contextlib.closing(sqlite3.connect(database)) as connection, connection:
                    if kind == "spec":
                        connection.execute("DELETE FROM records WHERE kind='AcceptanceSpec' AND id=? AND version=?",
                                           (report["spec_ref"]["id"], report["spec_ref"]["version"]))
                    elif kind == "receipts":
                        connection.execute("DELETE FROM blobs WHERE blob_id=?", ("acceptance-evidence:" + report["report_id"],))
                    else:
                        connection.execute("DELETE FROM records WHERE kind='AcceptanceReport' AND id=?", (report["report_id"],))
                with self.assertRaises(StudioError) as error:
                    self.service.detail(run_id)
                self.assertEqual(error.exception.code, "storage_error")
                summary = next(run for run in self.service.list_runs()["runs"] if run["id"] == run_id)
                self.assertNotEqual(summary["status"], "completed")

    def test_newer_spec_version_does_not_replace_report_bound_version(self):
        run_id, detail = self.demo()
        original = detail["acceptance"][0]["spec"]
        database = self.root / "runs" / run_id / "tasks.sqlite3"
        newer = {**original, "version": original["version"] + 1, "objective": "a later independent version"}
        with contextlib.closing(sqlite3.connect(database)) as connection, connection:
            row = connection.execute("SELECT revision,task_id FROM records WHERE kind='AcceptanceSpec' AND id=? AND version=?",
                                     (original["spec_id"], original["version"])).fetchone()
            connection.execute("INSERT INTO records(kind,id,version,revision,task_id,payload) VALUES(?,?,?,?,?,?)",
                               ("AcceptanceSpec", original["spec_id"], newer["version"], row[0], row[1], json.dumps(newer)))
        result = self.service.detail(run_id)["acceptance"][0]
        self.assertEqual(result["spec"], original)
        validate_evidence(AcceptanceSpec.model_validate(result["spec"]),
                          AcceptanceReport.model_validate(result["report"]), result["receipts"])

    def test_unsupported_or_incomplete_schema_never_reports_historical_success(self):
        for corruption in ("version", "missing_table"):
            with self.subTest(corruption=corruption):
                run_id, _ = self.demo()
                database = self.root / "runs" / run_id / "tasks.sqlite3"
                with contextlib.closing(sqlite3.connect(database)) as connection, connection:
                    connection.execute("PRAGMA user_version=999" if corruption == "version" else "DROP TABLE outbox")
                with self.assertRaises(StudioError):
                    self.service.detail(run_id)
                summary = next(run for run in self.service.list_runs()["runs"] if run["id"] == run_id)
                self.assertNotEqual(summary["status"], "completed")
                with contextlib.closing(sqlite3.connect(database)) as connection, connection:
                    if corruption == "version":
                        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 999)
                    else:
                        self.assertEqual(connection.execute("SELECT name FROM sqlite_master WHERE name='outbox'").fetchall(), [])

    def test_large_log_returns_latest_complete_events_and_marks_partial_metrics(self):
        run_id, _ = self.demo()
        path = self.root / "runs" / run_id / "trace.jsonl"
        with path.open("w", encoding="utf-8") as stream:
            for index in range(40):
                stream.write(json.dumps({"kind": "observation", "index": index, "content": "padding" * 20}) + "\n")
            stream.write(json.dumps({"kind": "latest-marker", "content": "the latest complete event"}) + "\n")
            stream.write('{"kind":"unfinished')
        with patch("miniagent.studio_service.MAX_LOG_BYTES", 1024):
            detail = self.service.detail(run_id)
        self.assertEqual(detail["events"][-1]["kind"], "latest-marker")
        self.assertTrue(detail["events_truncated"])
        self.assertTrue(detail["summary_incomplete"])
        self.assertEqual(detail["run"]["status"], "completed")
        self.assertEqual(detail["tasks"][0]["status"], "completed")

    def test_display_event_limit_does_not_claim_metrics_are_partial(self):
        run_id, _ = self.demo()
        path = self.root / "runs" / run_id / "trace.jsonl"
        with path.open("a", encoding="utf-8") as stream:
            for index in range(350):
                stream.write(json.dumps({"kind": "observation", "index": index}) + "\n")
        detail = self.service.detail(run_id)
        self.assertEqual(len(detail["events"]), 300)
        self.assertEqual(detail["events"][-1]["index"], 349)
        self.assertTrue(detail["events_truncated"])
        self.assertFalse(detail["summary_incomplete"])
        self.assertGreater(detail["summary"]["total_tokens"], 0)

    def test_artifact_preview_denies_control_files_hidden_files_and_traversal(self):
        run_id, detail = self.demo()
        folder = Path(detail["artifacts"][0]["path"]).parent
        secret = self.root / "runs" / run_id / folder / ".env"
        secret.write_text("SYNTHETIC_SECRET=do-not-expose")
        for path in ["tasks.sqlite3", "studio.json", "../outside.txt", "/etc/passwd", str(folder / ".env"),
                     str(folder / ".." / "other.txt")]:
            with self.subTest(path=path), self.assertRaises(StudioError):
                self.service.artifact(run_id, path)
        self.assertNotIn(".env", [a["name"] for a in self.service.detail(run_id)["artifacts"]])

    def test_artifact_preview_rejects_symlink_file_and_parent(self):
        run_id, detail = self.demo()
        folder = Path(detail["artifacts"][0]["path"]).parent
        base = self.root / "runs" / run_id
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.md").write_text("synthetic secret")
        (base / folder / "link.md").symlink_to(outside / "secret.md")
        (base / folder / "linked").symlink_to(outside, target_is_directory=True)
        for path in [folder / "link.md", folder / "linked" / "secret.md"]:
            with self.subTest(path=path), self.assertRaises(StudioError):
                self.service.artifact(run_id, str(path))
        self.assertNotIn("link.md", [a["name"] for a in self.service.detail(run_id)["artifacts"]])

    def test_large_artifact_is_bounded_and_marks_truncation(self):
        run_id, detail = self.demo()
        path = Path(detail["artifacts"][0]["path"]).parent / "large.txt"
        (self.root / "runs" / run_id / path).write_text("界" * 200_000)
        preview = self.service.artifact(run_id, str(path))
        self.assertTrue(preview["truncated"])
        self.assertLessEqual(len(preview["content"].encode("utf-8")), 262147)

    def test_run_directory_symlink_cannot_expand_visible_scope(self):
        outside = self.root / "outside-run"
        outside.mkdir()
        (outside / "tasks.sqlite3").write_text("not accessible")
        (self.root / "runs" / "linked-run").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(StudioError):
            self.service.detail("linked-run")
        self.assertNotIn("linked-run", {r["id"] for r in self.service.list_runs()["runs"]})

    def test_bootstrap_exposes_readiness_without_provider_secret(self):
        service = StudioService(self.root / "configured", Settings(api_key="synthetic-key-not-for-browser"))
        try:
            value = service.bootstrap()
            self.assertTrue(value["provider_ready"])
            self.assertNotIn("synthetic-key-not-for-browser", json.dumps(value))
            self.assertNotIn("api_key", value)
        finally:
            service.close()

    def test_diagnostics_preserve_the_run_configuration_not_current_settings(self):
        run_id, detail = self.demo()
        self.assertEqual(detail["diagnostics"]["configuration"]["max_output_tokens"], 300_000)
        self.assertEqual(detail["diagnostics"]["acceptance_status"], "passed")
        self.service.close()
        self.service = StudioService(self.root / "runs", Settings(max_output_tokens=8192))
        self.assertEqual(self.service.bootstrap()["max_output_tokens"], 8192)
        self.assertEqual(self.service.detail(run_id)["diagnostics"]["configuration"]["max_output_tokens"], 300_000)

    def test_code_artifacts_are_returned_as_text_instead_of_hidden(self):
        run_id, detail = self.demo()
        directory = self.root / "runs" / run_id / Path(detail["artifacts"][0]["path"]).parent
        source = '<script>window.secret = "never execute this";</script>'
        for name in ("snake.html", "game.js", "style.css"):
            (directory / name).write_text(source)
        artifacts = self.service.detail(run_id)["artifacts"]
        for name in ("snake.html", "game.js", "style.css"):
            artifact = next(item for item in artifacts if item["name"] == name)
            self.assertEqual(self.service.artifact(run_id, artifact["path"])["content"], source)

    def test_worker_error_exposes_location_without_exception_message_or_locals(self):
        def broken(*args):
            secret_local = "synthetic-secret-in-exception"
            raise RuntimeError(secret_local)
        with patch.object(self.service, "_execute", side_effect=broken):
            run = self.service.start(request())
            detail = finished(self.service, run["id"])
        error = next(event for event in detail["events"] if event["kind"] == "studio_error")
        self.assertEqual(error["error_code"], "RuntimeError")
        self.assertTrue(any(frame["function"] == "broken" for frame in error["frames"]))
        self.assertTrue(all(set(frame) == {"file", "function", "line"} for frame in error["frames"]))
        self.assertNotIn("synthetic-secret-in-exception", json.dumps(detail))


if __name__ == "__main__":
    unittest.main()
