"""CLI acceptance configuration and complete batch denominators."""
from __future__ import annotations

import contextlib
import io
import json
import unittest
from pathlib import Path

from miniagent.cli import _execute, _load_tasks, main
from miniagent.config import Settings
from miniagent.metrics import load_events, summarize
from miniagent.providers import ScriptedProvider
from tests.support import cleanup, make_temp_dir

REPO = Path(__file__).resolve().parents[1]


class CliAcceptanceTest(unittest.TestCase):
    def setUp(self):
        self.base = make_temp_dir("cli-acceptance")

    def tearDown(self):
        cleanup(self.base)

    def invoke(self, args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = main(args + ["--runs-dir", str(self.base / "runs")])
        return code, output.getvalue()

    def config(self, name, payload):
        path = self.base / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def events(self):
        return load_events(next((self.base / "runs").iterdir()))

    def test_run_with_explicit_content_check_is_verified(self):
        config = self.config("checks.json", {"files": [{"path": "result.md", "contains": "全程离线"}]})
        code, output = self.invoke(["run", "生成离线说明 result.md", "--mock", str(REPO / "examples/mock_task.json"), "--acceptance", config])
        self.assertEqual(code, 0, output)
        self.assertEqual(summarize(self.events())["completed"], 1)

    def test_wrong_expected_content_is_not_success(self):
        config = self.config("checks.json", {"files": [{"path": "result.md", "contains": "expected-value-that-is-not-written"}]})
        code, output = self.invoke(["run", "目标内容", "--mock", str(REPO / "examples/mock_task.json"), "--acceptance", config])
        self.assertEqual(code, 1, output)
        self.assertEqual(summarize(self.events())["completed"], 0)

    def test_batch_per_case_acceptance_and_unverified_are_distinct(self):
        tasks = self.base / "tasks.jsonl"
        tasks.write_text("\n".join(json.dumps(item) for item in [
            {"task_id": "verified", "task": "生成离线说明", "acceptance": {"files": [{"path": "result.md", "contains": "全程离线"}]}},
            {"task_id": "unchecked", "task": "同一候选但未配置验收"},
        ]), encoding="utf-8")
        code, output = self.invoke(["eval", "--tasks", str(tasks), "--mock", str(REPO / "examples/mock_task.json")])
        self.assertEqual(code, 1, output)
        summary = summarize(self.events())
        self.assertEqual((summary["completed"], summary["total_tasks"]), (1, 2))
        self.assertIn("50.0%", output)

    def test_provider_initialization_failure_does_not_lose_remaining_cases(self):
        calls = 0
        def factory():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("provider setup failed")
            return ScriptedProvider([{"text": "candidate"}])
        with contextlib.redirect_stdout(io.StringIO()):
            code = _execute(
                [{"task_id": "first", "task": "first"}, {"task_id": "second", "task": "second"}],
                factory, self.base / "batch", Settings(), "worker", False,
                acceptance=lambda _ws: (True, "trusted fixture"),
            )
        self.assertEqual(code, 1)
        events = load_events(self.base / "batch")
        self.assertEqual([event["kind"] for event in events[:2]], ["case_registered", "case_registered"])
        summary = summarize(events)
        self.assertEqual((summary["completed"], summary["total_tasks"]), (1, 2))

    def test_invalid_per_case_checks_fail_only_that_case(self):
        tasks = self.base / "tasks.jsonl"
        tasks.write_text("\n".join(json.dumps(item) for item in [
            {"task_id": "bad", "task": "first", "acceptance": {"files": [{"path": "../secret"}]}},
            {"task_id": "next", "task": "second"},
        ]))
        code, output = self.invoke(["eval", "--tasks", str(tasks), "--mock", str(REPO / "examples/mock_task.json")])
        self.assertEqual(code, 1, output)
        summary = summarize(self.events())
        self.assertEqual(summary["total_tasks"], 2)
        self.assertEqual(summary["completed"], 0)

    def test_duplicate_or_malformed_manifest_cannot_shrink_denominator(self):
        path = self.base / "bad.jsonl"
        for text in ('{"task_id":"same","task":"a"}\n{"task_id":"same","task":"b"}', '{"task":"valid"}\nnot-json', '[]', '{"task":false}'):
            with self.subTest(text=text):
                path.write_text(text)
                with self.assertRaises(ValueError):
                    _load_tasks(path)

    def test_configuration_failure_still_records_all_eval_cases(self):
        tasks = self.base / "tasks.jsonl"
        tasks.write_text('{"task_id":"a","task":"first"}\n{"task_id":"b","task":"second"}')
        config = self.config("bad-checks.json", {"files": [], "unexpected": True})
        code, _ = self.invoke(["eval", "--tasks", str(tasks), "--mock", str(REPO / "examples/mock_task.json"), "--acceptance", config])
        self.assertEqual(code, 2)
        self.assertEqual(summarize(self.events())["total_tasks"], 2)

    def test_orchestrated_root_checks_its_own_target_content(self):
        config = self.config("checks.json", {
            "subtasks": {
                "s1": {"files": [{"path": "notes.md", "contains": "规划必须校验"}]},
                "s2": {"files": [{"path": "summary.md", "contains": "串行调度"}]},
            },
            "root": {"files": [
                {"path": "agents/task-s1/notes.md", "contains": "规划必须校验"},
                {"path": "agents/task-s2/summary.md", "contains": "缩短墙钟"},
            ]},
        })
        code, output = self.invoke(["orchestrate", "整理笔记及摘要", "--mock", str(REPO / "examples/mock_plan.json"), "--acceptance", config])
        self.assertEqual(code, 0, output)
        summary = summarize(self.events())
        self.assertEqual((summary["completed"], summary["total_tasks"]), (1, 1))
        self.assertEqual((summary["subtask_completed"], summary["subtask_total"]), (2, 2))


if __name__ == "__main__":
    unittest.main()
