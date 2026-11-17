"""A02: completion requires independent evidence, not a model's final answer."""
from __future__ import annotations

import hashlib
import unittest
from unittest.mock import patch

from miniagent.acceptance import accept_files
from miniagent.agent import Agent
from miniagent.config import Settings
from miniagent.contracts import AcceptanceReport, validate_report
from miniagent.metrics import Tracer, load_events
from miniagent.providers import ScriptedProvider
from miniagent.sandbox import Sandbox
from miniagent.schemas import LLMResponse, RunResult, Usage
from miniagent.tools import build_default_registry
from tests.support import cleanup, make_temp_dir


class AgentAcceptanceTest(unittest.TestCase):
    def setUp(self):
        self.base = make_temp_dir("agent-acceptance")

    def tearDown(self):
        cleanup(self.base)

    def run_script(self, script, acceptance=None, **kwargs):
        with Tracer(self.base) as tracer:
            agent = Agent(
                name="worker", provider=ScriptedProvider(script),
                registry=build_default_registry(), sandbox=Sandbox(self.base / "workspace"),
                settings=Settings(), tracer=tracer,
            )
            return agent.run("修复启动配置", task_id="repair", acceptance=acceptance, **kwargs)

    def test_missing_checker_preserves_answer_but_never_succeeds(self):
        result = self.run_script([{"text": "已经完全修好了"}, {"text": "不应再调用"}])
        self.assertEqual(result.status, "unverified")
        self.assertEqual(result.answer, "已经完全修好了")
        self.assertEqual(result.steps, 1)
        self.assertFalse(result.succeeded)
        self.assertIsNone(result.acceptance_spec)
        self.assertEqual(result.acceptance_report.verdict, "unverified")

    def test_claim_without_file_fails_and_retains_each_report(self):
        result = self.run_script(
            [{"text": "已修复"}, {"text": "肯定修好了"}],
            accept_files("launch.txt", min_bytes=1, contains={"launch.txt": "python=project-venv"}),
        )
        self.assertEqual(result.status, "failed_acceptance")
        reports = [e["report"] for e in load_events(self.base) if e["kind"] == "acceptance"]
        self.assertEqual(len(reports), 2)
        self.assertEqual(len({r["report_id"] for r in reports}), 2)
        self.assertEqual({r["attempt_id"] for r in reports}, {result.attempt_id})
        self.assertEqual(len({r["spec_ref"]["id"] for r in reports}), 1)
        self.assertTrue(all(r["verdict"] == "failed" for r in reports))

    def test_wrong_content_fails_even_when_size_is_sufficient(self):
        result = self.run_script([
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "launch.txt", "content": "python=wrong-system-interpreter"}}]},
            {"text": "完成"}, {"text": "完成"},
        ], accept_files("launch.txt", contains={"launch.txt": "python=project-venv"}))
        self.assertEqual(result.status, "failed_acceptance")
        self.assertFalse(result.succeeded)

    def test_repair_then_recheck_captures_final_content_and_one_attempt(self):
        content = "python=project-venv\n"
        result = self.run_script([
            {"text": "完成"},
            {"tool_calls": [{"name": "write_file", "arguments": {"path": "launch.txt", "content": content}}]},
            {"text": "现在完成"},
        ], accept_files("launch.txt", min_bytes=1, contains={"launch.txt": "python=project-venv"}))
        self.assertTrue(result.succeeded, result.error)
        validate_report(result.acceptance_spec, result.acceptance_report)
        self.assertEqual(result.acceptance_report.evaluated_resource_versions[0].version,
                         "sha256:" + hashlib.sha256(content.encode()).hexdigest())
        self.assertTrue(result.acceptance_evidence)
        events = load_events(self.base)
        reports = [e["report"] for e in events if e["kind"] == "acceptance"]
        self.assertEqual([r["verdict"] for r in reports], ["failed", "passed"])
        self.assertEqual({r["attempt_id"] for r in reports}, {result.attempt_id})
        for event in events:
            self.assertEqual(event["schema_version"], 2)
            self.assertEqual(event["root_id"], "repair")
            self.assertEqual(event["task_id"], "repair")
        self.assertEqual(AcceptanceReport.model_validate(reports[-1]), result.acceptance_report)

    def test_checker_exception_is_error_without_model_repair_retry(self):
        def broken(_workspace):
            raise RuntimeError("checker unavailable")
        result = self.run_script([{"text": "完成"}, {"text": "不应再调用"}], broken)
        self.assertEqual(result.status, "error")
        self.assertEqual(result.acceptance_report.verdict, "error")
        self.assertEqual(result.steps, 1)
        self.assertIn("checker unavailable", result.error)

    def test_truthy_checker_result_is_not_success(self):
        result = self.run_script([{"text": "完成"}], lambda _ws: ("false", "invalid"))
        self.assertEqual(result.status, "error")
        self.assertFalse(result.succeeded)

    def test_child_trace_preserves_root_parent_and_attempt(self):
        result = self.run_script([{"text": "候选结果"}], root_id="root", parent_task_id="parent")
        for event in load_events(self.base):
            self.assertEqual(event["level"], "subtask")
            self.assertEqual(event["root_id"], "root")
            self.assertEqual(event["parent_task_id"], "parent")
            self.assertEqual(event["attempt_id"], result.attempt_id)

    def test_legacy_completed_result_does_not_claim_verified_success(self):
        old = RunResult(task_id="legacy", agent="worker", status="completed")
        self.assertFalse(old.succeeded)

    def test_explicit_root_identity_is_not_misclassified_as_child(self):
        self.run_script([{"text": "candidate"}], root_id="repair")
        self.assertTrue(all(event["level"] == "goal" for event in load_events(self.base)))

    def test_observer_failure_keeps_record_of_completed_model_call(self):
        def broken_observer(_record, _state):
            raise RuntimeError("observer failed")
        response = LLMResponse(text="candidate", usage=Usage(prompt_tokens=11, completion_tokens=4))
        with patch.object(ScriptedProvider, "chat", return_value=response):
            result = self.run_script([], on_step=broken_observer)
        self.assertEqual(result.status, "error")
        steps = [event for event in load_events(self.base) if event["kind"] == "step"]
        self.assertEqual(len(steps), 1)
        self.assertEqual(result.usage.total, 15)

    def test_result_without_resolvable_receipts_is_not_verified(self):
        result = self.run_script([{"text": "ok"}], lambda _ws: (True, "fixture"))
        self.assertTrue(result.succeeded)
        self.assertFalse(RunResult.model_validate({**result.model_dump(), "acceptance_evidence": []}).succeeded)

    def test_passed_report_for_another_task_or_attempt_is_not_success(self):
        result = self.run_script([{"text": "ok"}], lambda _ws: (True, "fixture"))
        self.assertTrue(result.succeeded)
        self.assertFalse(RunResult.model_validate({**result.model_dump(), "task_id": "other"}).succeeded)
        self.assertFalse(RunResult.model_validate({**result.model_dump(), "attempt_id": "other"}).succeeded)


if __name__ == "__main__":
    unittest.main()
