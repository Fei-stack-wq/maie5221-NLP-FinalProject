"""Harness controls must work through Studio and retain usable artifacts."""
from __future__ import annotations

import io
import json
import threading
import time
import unittest
import zipfile
from pathlib import PurePosixPath
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.providers import ScriptedProvider
from miniagent.studio_service import StudioError, StudioService
from miniagent.task_store import TaskStore
from tests.support import cleanup, make_temp_dir
from tests.test_studio_service import finished, request


class HeldProvider(ScriptedProvider):
    """Hold an actual runtime provider boundary; no network or tool mocks."""

    def __init__(self, script):
        super().__init__(script)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def chat(self, messages, tools=None, temperature=0.0):
        self.calls += 1
        self.entered.set()
        if not self.release.wait(5):
            raise RuntimeError("test provider was not released")
        return super().chat(messages, tools, temperature)


class StudioHarnessTest(unittest.TestCase):
    def test_bundle_rejects_binary_disguised_as_text_instead_of_corrupting_bytes(self):
        run_id = self.demo()[0]
        path = self.root / "runs" / run_id / "agents" / "worker" / "bad.txt"
        for content in (b"bad\xffencoding", b"binary\x00payload"):
            with self.subTest(content=content):
                path.write_bytes(content)
                with self.assertRaises(StudioError) as raised:
                    self.service.artifact_bundle(run_id)
                self.assertEqual((raised.exception.code, raised.exception.status), ("unsupported_file", 415))

    def test_valid_screenshot_roundtrips_and_invalid_or_credential_files_reject(self):
        from PIL import Image, PngImagePlugin
        run_id = self.demo()[0]
        name = "agents/worker/page.png"
        target = self.root / "runs" / run_id / name
        image = Image.new("RGB", (30, 40), "teal")
        image.save(target)
        source = target.read_bytes()
        self.assertEqual(self.service.artifact(run_id, name)["media_type"], "image/png")
        self.assertEqual(self.service.download_artifact(run_id, name), source)
        with self.archive(run_id) as bundle:
            self.assertEqual(bundle.read("page.png"), source)
        meta = PngImagePlugin.PngInfo()
        meta.add_text("private", "synthetic-test-key", zip=True)
        image.save(target, pnginfo=meta)
        sensitive_png = target.read_bytes()
        for bad in (b"fake screenshot", source+b"appended", sensitive_png):
            target.write_bytes(bad)
            with self.assertRaises(StudioError):
                self.service.download_artifact(run_id, name)

    def setUp(self):
        self.root = make_temp_dir("studio-harness")
        self.service = StudioService(self.root / "runs", Settings(api_key="synthetic-test-key"))

    def tearDown(self):
        self.service.close()
        cleanup(self.root)

    def settle(self, run_id):
        # A durable task can settle just before the worker releases its slot.
        finished(self.service, run_id)
        deadline = time.monotonic() + 5
        while self.service.list_runs()["active_count"] and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.service.list_runs()["active_count"], 0)
        return self.service.detail(run_id)

    def demo(self, mode="demo"):
        run = self.service.start(request(mode))
        detail = self.settle(run["id"])
        self.assertEqual(detail["run"]["status"], "completed", detail["run"])
        return run["id"], detail

    def archive(self, run_id):
        return zipfile.ZipFile(io.BytesIO(self.service.artifact_bundle(run_id)))

    def test_per_run_limits_affect_execution_and_snapshot_without_changing_defaults(self):
        limits = {"max_steps": 2, "max_total_tokens": 200000, "max_seconds": 75}
        run = self.service.start(request(limits=limits))
        detail = self.settle(run["id"])
        self.assertEqual(detail["run"]["status"], "failed")
        ends = [event for event in detail["events"] if event["kind"] == "task_end"]
        self.assertEqual(ends[-1]["status"], "limit_exceeded")
        self.assertEqual(ends[-1]["steps"], 2)
        config = next(event["configuration"] for event in detail["events"]
                      if event["kind"] == "studio_configuration")
        self.assertEqual(config["max_steps"], limits["max_steps"])
        self.assertEqual(config["max_task_seconds"], limits["max_seconds"])
        self.assertEqual(config["max_total_tokens"], limits["max_total_tokens"])
        # A second task uses the host default and reaches independent acceptance.
        _, default_detail = self.demo()
        default_config = next(event["configuration"] for event in default_detail["events"]
                              if event["kind"] == "studio_configuration")
        self.assertEqual(default_config["max_steps"], 0)
        self.assertEqual(default_config["max_total_tokens"], 0)
        self.assertEqual(self.service.settings.limits.max_steps, 0)
        self.assertEqual(self.service.detail(run["id"])["run"]["status"], "failed")
        with self.archive(run["id"]) as archive:
            self.assertIn("notes.md", archive.namelist())

    def test_invalid_budget_ranges_and_types_do_not_create_runs(self):
        invalid = [None, [], {"unknown": 2}, {"max_steps": -1}, {"max_steps": 1001},
                   {"max_steps": True}, {"max_steps": 2.5}, {"max_steps": "20"},
                   {"max_total_tokens": -1}, {"max_total_tokens": 100000001},
                   {"max_total_tokens": False}, {"max_seconds": 0}, {"max_seconds": 86401},
                   {"max_seconds": float("inf")}, {"max_seconds": float("nan")},
                   {"max_seconds": True}]
        for limits in invalid:
            with self.subTest(limits=limits), self.assertRaises(StudioError) as raised:
                self.service.start(request(limits=limits))
            self.assertEqual(raised.exception.status, 400)
        self.assertEqual(self.service.list_runs()["runs"], [])

    def test_single_cancel_while_waiting_prevents_late_tool_and_persists_once(self):
        provider = HeldProvider([{"tool_calls": [{"name": "write_file", "arguments": {
            "path": "late.txt", "content": "must not be written"}}]}])
        with patch("miniagent.studio_service.DeepSeekProvider", autospec=True, return_value=provider):
            try:
                run = self.service.start(request("single", objective="write late.txt"))
                self.assertTrue(provider.entered.wait(2))
                with self.assertRaises(StudioError) as active_export:
                    self.service.artifact_bundle(run["id"])
                self.assertEqual(active_export.exception.code, "run_active")
                first = self.service.cancel(run["id"])
                second = self.service.cancel(run["id"])
                self.assertTrue(first["cancel_requested"])
                self.assertTrue(second["cancel_requested"])
                self.assertNotEqual(second["status"], "cancelled")
                self.assertEqual(self.service.list_runs()["active_count"], 1)
            finally:
                provider.release.set()
            detail = self.settle(run["id"])
        self.assertEqual(detail["run"]["status"], "cancelled", detail["run"])
        self.assertEqual(provider.calls, 1)
        self.assertFalse((self.root / "runs" / run["id"] / "agents" / "worker" / "late.txt").exists())
        self.assertEqual(detail["artifacts"], [])
        store = TaskStore(self.root / "runs" / run["id"] / "tasks.sqlite3")
        before = store.get(run["id"])
        self.assertEqual(before.status, "cancelled")
        self.assertEqual(len(store.attempts(run["id"])), 1)
        self.service.cancel(run["id"])
        self.service.cancel(run["id"])
        self.assertEqual(store.get(run["id"]), before)
        self.assertEqual(self.service.detail(run["id"])["events"], detail["events"])

    def test_team_cancel_during_plan_creates_no_children(self):
        plan = {"subtasks": [{"id": "s1", "objective": "write late.txt", "assignee": "worker",
                              "deps": [], "acceptance": "late.txt exists"}]}
        provider = HeldProvider([{"text": json.dumps(plan)}])
        with patch("miniagent.studio_service.DeepSeekProvider", autospec=True, return_value=provider):
            try:
                run = self.service.start(request("team", objective="build a small project"))
                self.assertTrue(provider.entered.wait(2))
                self.service.cancel(run["id"])
            finally:
                provider.release.set()
            detail = self.settle(run["id"])
        self.assertEqual(detail["run"]["status"], "cancelled", detail["run"])
        self.assertEqual(len(detail["tasks"]), 1)
        self.assertIsNone(detail["tasks"][0]["parent_id"])
        self.assertEqual(detail["tasks"][0]["status"], "cancelled")
        self.assertEqual(provider.calls, 1)
        self.assertEqual(detail["artifacts"], [])

    def test_cancel_completed_run_preserves_acceptance_and_events(self):
        run_id, before = self.demo()
        self.assertEqual(self.service.cancel(run_id)["status"], "completed")
        self.assertEqual(self.service.cancel(run_id)["status"], "completed")
        after = self.service.detail(run_id)
        self.assertEqual(before["tasks"], after["tasks"])
        self.assertEqual(before["acceptance"], after["acceptance"])
        self.assertEqual(before["events"], after["events"])
        self.assertFalse(after["run"]["cancel_requested"])

    def test_single_zip_retains_relative_references_and_nested_test_paths(self):
        run_id, _ = self.demo()
        workspace = self.root / "runs" / run_id / "agents" / "worker"
        html = '<link rel="stylesheet" href="assets/style.css"><script src="game.js"></script>'
        (workspace / "assets").mkdir()
        (workspace / "test").mkdir()
        (workspace / "index.html").write_text(html)
        (workspace / "assets" / "style.css").write_text("body { color: blue }")
        (workspace / "game.js").write_text("globalThis.ready = true;")
        (workspace / "test" / "smoke.js").write_text("require('../game.js');")
        with self.archive(run_id) as archive:
            names = set(archive.namelist())
            self.assertEqual(names, {"notes.md", "index.html", "assets/style.css", "game.js", "test/smoke.js"})
            self.assertEqual(archive.read("index.html").decode(), html)
            self.assertEqual(archive.read("test/smoke.js").decode(), "require('../game.js');")

    def test_team_zip_keeps_task_directories(self):
        run_id, detail = self.demo("team-demo")
        with self.archive(run_id) as archive:
            names = archive.namelist()
            self.assertEqual({PurePosixPath(name).name for name in names}, {"notes.md", "summary.md"})
            self.assertTrue(all(len(PurePosixPath(name).parts) >= 2 for name in names))
            self.assertEqual(len({PurePosixPath(name).parts[0] for name in names}), 2)
            self.assertEqual(len(names), len(detail["artifacts"]))

    def test_zip_excludes_control_hidden_binary_and_linked_files(self):
        run_id, _ = self.demo()
        directory = self.root / "runs" / run_id
        workspace = directory / "agents" / "worker"
        outside = self.root / "outside.txt"
        outside.write_text("outside-secret")
        (workspace / "leak.txt").symlink_to(outside)
        (workspace / "linked-dir").symlink_to(self.root, target_is_directory=True)
        (workspace / ".secret.txt").write_text("hidden-secret")
        (workspace / ".hidden").mkdir()
        (workspace / ".hidden" / "nested.txt").write_text("hidden-nested")
        (workspace / "preview.unknown").write_bytes(b"unsupported binary artifact")
        (directory / "control.txt").write_text("control-secret")
        with self.archive(run_id) as archive:
            self.assertEqual(archive.namelist(), ["notes.md"])
            self.assertNotIn(b"secret", archive.read("notes.md"))

    def test_zip_rejects_oversize_file_instead_of_truncating(self):
        run_id, _ = self.demo()
        workspace = self.root / "runs" / run_id / "agents" / "worker"
        (workspace / "large.txt").write_bytes(b"x" * (8 * 1024 * 1024 + 1))
        with self.assertRaises(StudioError) as raised:
            self.service.artifact_bundle(run_id)
        self.assertEqual((raised.exception.code, raised.exception.status), ("bundle_too_large", 413))

    def test_zip_rejects_aggregate_oversize(self):
        run_id, _ = self.demo()
        workspace = self.root / "runs" / run_id / "agents" / "worker"
        for index in range(3):
            (workspace / f"large-{index}.txt").write_bytes(b"x" * (7 * 1024 * 1024))
        with self.assertRaises(StudioError) as raised:
            self.service.artifact_bundle(run_id)
        self.assertEqual((raised.exception.code, raised.exception.status), ("bundle_too_large", 413))


if __name__ == "__main__":
    unittest.main()
