"""Diagnostics explain observed failures without rewriting historical truth."""
from __future__ import annotations

import copy
import json
import time
import unittest
import uuid
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.studio_diagnostics import diagnose
from miniagent.studio_service import StudioService
from tests.support import cleanup, make_temp_dir


def task(identity="root", status="unverified", parent=None):
    return {"task_id": identity, "status": status, "parent_id": parent}


def report(verdict="unverified", spec=None, identity="root"):
    return {"task_id": identity, "spec": spec,
            "report": {"verdict": verdict, "reason": "check explanation"}}


def event(kind, identity="root", **fields):
    return {"kind": kind, "task_id": identity, **fields}


class StudioDiagnosticsTest(unittest.TestCase):
    def test_accepted_root_after_repair_retains_child_issue(self):
        result = diagnose([task(status="completed"), task("draft", "failed", parent="root")],
                          [event("task_end", "draft", status="failed", error="missing deliverable"),
                           event("task_end", status="completed")], [report("passed", {"id": "check"})])
        self.assertEqual(result["execution_status"], "finished")
        self.assertEqual(result["issues"][0]["task_id"], "draft")
        self.assertEqual(result["issues"][0]["severity"], "warning")

    def test_live_activity_exposes_runtime_phase(self):
        result = diagnose([], [event("step_start", step=4, phase="handoff")], [], active=True)
        self.assertEqual(result["current_activity"][0]["phase"], "handoff")

    def test_step_limit_is_distinct_from_model_output_truncation(self):
        events = [event("studio_configuration", configuration={"max_steps": 12, "max_output_tokens": 300000}),
                  event("step", step=12, finish_reason="tool_calls", completion_tokens=894),
                  event("task_end", status="limit_exceeded", error="超过最大步数 12")]
        result = diagnose([task(status="failed")], events, [])
        self.assertEqual(result["issues"][0]["code"], "step_limit")
        self.assertEqual(result["configuration"]["max_steps"], 12)
        self.assertFalse(any(issue["code"] == "output_truncated" for issue in result["issues"]))

    def test_other_runtime_limits_are_not_misreported_as_timeouts(self):
        for reason, code in [("检测到重复动作（T_LOOP）", "loop_limit"), ("超过动作总数上限", "action_limit"),
                             ("未知运行限制", "runtime_limit"), ("超过时间上限 3600s", "time_budget")]:
            with self.subTest(code=code):
                value = diagnose([task(status="failed")], [event("task_end", status="limit_exceeded", error=reason)], [])
                self.assertEqual(value["issues"][0]["code"], code)

    def test_cumulative_token_incident_explains_historical_prompt_and_output_totals(self):
        value = diagnose([task(status="failed")], [
            event("task_end", status="limit_exceeded", error="超过 token 预算 1000000",
                  prompt_tokens=1010961, completion_tokens=24388)
        ], [])
        issue = value["issues"][0]
        self.assertEqual(issue["code"], "token_budget")
        for count in ("1,010,961", "24,388", "1,035,349"):
            self.assertIn(count, issue["message"])

    def test_cancelled_task_is_not_presented_as_an_execution_error(self):
        result = diagnose([task(status="cancelled")],
                          [event("task_end", status="aborted", error="用户请求停止")], [])
        self.assertEqual(result["execution_status"], "cancelled")
        self.assertEqual(result["issues"], [])

    def test_complete_response_after_recovery_does_not_hide_later_step_limit(self):
        events = [event("step", step=1, finish_reason="length", attempt_id="a", run_id="r"),
                  event("step", step=2, finish_reason="tool_calls", attempt_id="a", run_id="r"),
                  event("task_end", status="limit_exceeded", error="超过最大步数 2", attempt_id="a", run_id="r")]
        result = diagnose([task(status="failed")], events, [])
        self.assertTrue(result["issues"][0]["resolved"])
        self.assertEqual(result["issues"][1]["code"], "step_limit")

    def test_original_incident_has_two_causes_without_upgrading_historical_status(self):
        tasks = [task(), task("root:s1", "failed", "root"), task("root:s2", "cancelled", "root")]
        events = [
            event("step", "root:s1", step=2, finish_reason="length", completion_tokens=4096,
                  latency_ms=22828.83, request_id="historical-request"),
            event("task_end", "root:s1", status="error", error="T_PROTOCOL"),
            event("subtask_end", "root:s2", status="blocked", error="上游未完成：s1"),
            event("task_end", status="unverified", error="未配置验收 (no-acceptance)"),
        ]
        reports = [report()]
        before = copy.deepcopy((tasks, events, reports))
        diagnostics = diagnose(tasks, events, reports)
        self.assertEqual(diagnostics["execution_status"], "failed")
        self.assertEqual(diagnostics["acceptance_status"], "not_configured")
        self.assertEqual({i["code"] for i in diagnostics["issues"]}, {"output_truncated", "no_acceptance"})
        truncated = diagnostics["issues"][0]
        self.assertEqual(truncated["completion_tokens"], 4096)
        self.assertFalse(truncated["resolved"])
        self.assertEqual(truncated["severity"], "error")
        # The old trace reports consumed tokens, not the configured ceiling.
        self.assertIsNone(truncated["max_output_tokens"])
        self.assertEqual(diagnostics["blocked_tasks"][0]["task_id"], "root:s2")
        self.assertEqual((tasks, events, reports), before)

    def test_new_recorded_output_limit_is_used_without_guessing_from_current_settings(self):
        events = [event("studio_configuration", configuration={"max_output_tokens": 1234}),
                  event("step", step=1, finish_reason="length", completion_tokens=1234),
                  event("task_end", status="error", error="truncated")]
        result = diagnose([task(status="failed")], events, [])
        self.assertEqual(result["issues"][0]["max_output_tokens"], 1234)

    def test_failed_acceptance_does_not_falsely_claim_execution_failed(self):
        result = diagnose([task(status="failed")],
                          [event("task_end", status="failed_acceptance", error="check failed")],
                          [report("failed", spec={"spec_id": "trusted-check"})])
        self.assertEqual(result["execution_status"], "finished")
        self.assertEqual(result["acceptance_status"], "failed")
        self.assertEqual([i["code"] for i in result["issues"]], ["acceptance_failed"])

    def test_missing_spec_does_not_turn_evaluator_error_into_no_acceptance(self):
        result = diagnose([task(status="failed")], [], [report("error")], incomplete=True)
        self.assertEqual(result["acceptance_status"], "error")
        self.assertEqual(result["execution_status"], "unknown")
        self.assertEqual(result["issues"][0]["code"], "acceptance_error")
        self.assertTrue(result["incomplete"])

    def test_parallel_activity_is_correlated_by_attempt_run_and_step(self):
        events = [
            event("plan_start", call_id="p1", attempt_id="root-a", run_id="root-r"),
            event("plan_usage", call_id="p1", attempt_id="root-a", run_id="root-r"),
            event("step_start", "a", step=1, attempt_id="a1", run_id="ra"),
            event("step_start", "b", step=1, attempt_id="b2", run_id="rb", ts=5, iso="2026-01-01T00:00:05Z"),
            event("step", "a", step=1, attempt_id="a1", run_id="ra"),
            # A late terminal from an old attempt cannot close b's new call.
            event("task_end", "b", attempt_id="b1", run_id="old", status="error"),
        ]
        result = diagnose([task(status="running")], events, [], active=True)
        self.assertEqual(len(result["current_activity"]), 1)
        self.assertEqual(result["current_activity"][0]["task_id"], "b")
        self.assertEqual(result["current_activity"][0]["phase"], "agent_step")
        self.assertEqual(result["current_activity"][0]["started_ts"], 5)
        events.append(event("provider_error", "b", step=1, attempt_id="b2", run_id="rb"))
        self.assertEqual(diagnose([], events, [], active=True)["current_activity"], [])

    def test_planning_error_matches_the_call_and_terminal_closes_unreturned_step(self):
        events = [event("plan_start", call_id="p1"), event("plan_start", call_id="p2"),
                  event("provider_error", call_id="p1"),
                  event("step_start", "child", step=1, attempt_id="a", run_id="r"),
                  event("task_end", "child", attempt_id="a", run_id="r", status="error")]
        current = diagnose([], events, [], active=True)["current_activity"]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["phase"], "planning")
        events.append(event("plan_usage", call_id="p2"))
        self.assertEqual(diagnose([], events, [], active=True)["current_activity"], [])

    def test_planner_truncation_is_explained_even_when_final_log_is_missing(self):
        result = diagnose([task(status="running")], [
            event("plan_usage", finish_reason="length", completion_tokens=4096, max_output_tokens=4096)
        ], [], incomplete=True)
        self.assertEqual(result["execution_status"], "failed")
        self.assertEqual(result["issues"][0]["code"], "output_truncated")
        self.assertIn("子任务未调度", result["issues"][0]["title"])

    def test_finished_child_does_not_prove_interrupted_root_finished(self):
        result = diagnose([task(status="running"), task("child", "completed", "root")],
                          [event("task_end", "child", status="completed")], [], incomplete=True)
        self.assertEqual(result["execution_status"], "unknown")
        self.assertEqual(result["current_activity"], [])

    def test_accepted_plan_after_truncation_is_a_resolved_historical_warning(self):
        events = [
            event("plan_usage", finish_reason="length", attempt_id="a", run_id="r"),
            event("plan_usage", finish_reason="stop", attempt_id="a", run_id="r"),
            {"kind": "plan", "root_id": "root", "root_attempt_id": "a", "root_run_id": "r"},
            event("task_end", status="completed", attempt_id="a", run_id="r"),
        ]
        result = diagnose([task(status="completed")], events, [report("passed", spec={})])
        self.assertEqual(result["execution_status"], "finished")
        self.assertEqual(result["acceptance_status"], "passed")
        self.assertEqual(len(result["issues"]), 1)
        issue = result["issues"][0]
        self.assertTrue(issue["resolved"])
        self.assertEqual(issue["severity"], "warning")
        self.assertNotIn("子任务未调度", issue["title"])

    def test_later_normal_response_without_accepted_plan_does_not_resolve_truncation(self):
        events = [event("plan_usage", finish_reason="length", attempt_id="a", run_id="r"),
                  event("plan_usage", finish_reason="stop", attempt_id="a", run_id="r"),
                  event("plan_invalid", attempt_id="a", run_id="r")]
        result = diagnose([task(status="running")], events, [])
        self.assertFalse(result["issues"][0]["resolved"])

    def test_conflicting_or_partially_missing_attempt_identity_does_not_claim_recovery(self):
        for first in ({"attempt_id": "a", "run_id": "r"}, {}):
            with self.subTest(first=first):
                events = [event("plan_usage", finish_reason="length", **first),
                          event("plan", attempt_id="other", run_id="other-run"),
                          event("task_end", status="completed", attempt_id="other", run_id="other-run")]
                result = diagnose([task(status="completed")], events, [report("passed", spec={})])
                self.assertFalse(result["issues"][0]["resolved"])

    def test_same_attempt_completion_resolves_step_truncation(self):
        events = [event("step", step=1, finish_reason="length", attempt_id="a", run_id="r"),
                  event("task_end", status="completed", attempt_id="a", run_id="r")]
        result = diagnose([task(status="completed")], events, [report("passed", spec={})])
        self.assertEqual(result["execution_status"], "finished")
        self.assertTrue(result["issues"][0]["resolved"])

    def test_recovered_plan_does_not_hide_a_subsequent_execution_error(self):
        events = [event("plan_usage", finish_reason="length", attempt_id="a", run_id="r"),
                  event("plan", attempt_id="a", run_id="r"),
                  event("task_end", status="error", error="worker error", attempt_id="a", run_id="r")]
        result = diagnose([task(status="failed")], events, [report()])
        self.assertEqual(result["execution_status"], "failed")
        self.assertTrue(result["issues"][0]["resolved"])
        errors = [issue for issue in result["issues"] if issue["severity"] == "error"]
        self.assertEqual([issue["code"] for issue in errors], ["execution_failed"])
        self.assertEqual(errors[0]["message"], "worker error")

    def test_service_exception_diagnostic_has_safe_frame_locations_without_secret_message(self):
        root = make_temp_dir("studio-diagnostic-error")
        service = StudioService(root, Settings(api_key="private-test-credential"))
        try:
            def fail(*_args):
                raise RuntimeError("private-test-credential https://private-endpoint.invalid/secret")

            with patch.object(service, "_execute", side_effect=fail):
                run = service.start({"request_id": str(uuid.uuid4()), "mode": "demo"})
                deadline = time.monotonic() + 3
                while True:
                    detail = service.detail(run["id"])
                    if detail["run"]["status"] == "failed":
                        break
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.01)
            diagnostic = detail["diagnostics"]
            self.assertEqual(diagnostic["execution_status"], "failed")
            issue = next(i for i in diagnostic["issues"] if i["code"] == "studio_error")
            self.assertEqual(issue["error_code"], "RuntimeError")
            self.assertTrue(issue["frames"])
            for frame in issue["frames"]:
                self.assertNotIn("/", frame["file"])
                self.assertEqual(set(frame), {"file", "function", "line"})
            raw_trace = (root / run["id"] / "trace.jsonl").read_text()
            for secret in ("private-test-credential", "private-endpoint.invalid"):
                self.assertNotIn(secret, json.dumps(detail))
                self.assertNotIn(secret, raw_trace)
        finally:
            service.close()
            cleanup(root)


if __name__ == "__main__":
    unittest.main()
