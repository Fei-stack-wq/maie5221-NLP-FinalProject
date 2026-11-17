"""Goal TCR, evidence binding, missing cases and trace replay regressions."""
from __future__ import annotations

import copy
import json
import unittest

from miniagent.metrics import load_events, render_report, summarize
from miniagent.acceptance_evidence import validate_evidence
from miniagent.contracts.acceptance import AcceptanceReport, AcceptanceSpec
from tests.support import cleanup, make_temp_dir
from tests.test_metrics import acceptance_fields, trace_event


def passed(task_id, root_id=None, attempt_id="a1", kind="task_end", **payload):
    return trace_event(kind, task_id, root_id=root_id, attempt_id=attempt_id,
                       status="completed", duration_ms=100, tool_calls=0,
                       **acceptance_fields(task_id, attempt_id), **payload)


class AcceptanceMetricsTest(unittest.TestCase):
    def test_child_success_does_not_make_root_success(self):
        events = [trace_event("case_registered", "g"), passed("s", "g", kind="subtask_end"),
                  trace_event("orchestration_end", "g", status="failed_acceptance",
                              **acceptance_fields("g", verdict="failed"), error="root target wrong", duration_ms=120)]
        result = summarize(events)
        self.assertEqual((result["goal_total"], result["goal_completed"]), (1, 0))
        self.assertEqual((result["subtask_total"], result["subtask_completed"]), (1, 1))
        self.assertEqual(result["goal_status"], "failed_acceptance")
        self.assertEqual(result["goals"][0]["report_id"], "report-g-a1")

    def test_no_checker_and_legacy_completion_are_unverified(self):
        events = [{"kind": "task_end", "task_id": "old", "status": "completed"},
                  trace_event("task_end", "new", status="completed")]
        result = summarize(events)
        self.assertEqual(result["completed"], 0)
        self.assertEqual(result["goal_status_counts"], {"unverified": 2})
        self.assertEqual(result["goal_status"], "multiple")

    def test_missing_crashed_and_not_started_cases_keep_denominator(self):
        events = [passed("ok"), trace_event("task_start", "crashed"),
                  trace_event("case_registered", "never-started")]
        result = summarize(events, expected_cases=["ok", "crashed", {"task_id": "never-started"}, {"id": "not-even-registered"}])
        self.assertEqual(result["total_tasks"], 4)
        self.assertEqual(result["completion_rate"], 0.25)
        self.assertEqual(result["goal_status_counts"], {"completed": 1, "incomplete": 1, "not_started": 2})
        self.assertEqual(result["duration_unknown_count"], 3)

    def test_multiple_roots_are_all_listed_instead_of_using_last(self):
        result = summarize([trace_event("orchestration_end", "first", status="error", error="planner crashed"),
                            passed("second", kind="orchestration_end")])
        self.assertEqual(result["goal_completion_rate"], 0.5)
        self.assertEqual([row["task_id"] for row in result["goals"]], ["first", "second"])
        self.assertEqual(result["goal_status"], "multiple")
        self.assertIsNone(result["goal_duration_ms"])

    def test_same_child_id_in_different_roots_is_two_logical_children(self):
        result = summarize([passed("s1", "g1", kind="subtask_end"), passed("s1", "g2", kind="subtask_end")])
        self.assertEqual(result["goal_total"], 2)
        self.assertEqual(result["subtask_total"], 2)
        self.assertEqual(result["subtask_completed"], 2)

    def test_registered_blocked_and_unstarted_children_remain_in_subtask_denominator(self):
        result = summarize([
            trace_event("task_registered", "s1", root_id="g", parent_task_id="g"),
            trace_event("task_registered", "s2", root_id="g", parent_task_id="g"),
            trace_event("subtask_end", "s1", root_id="g", status="blocked", error="upstream unverified"),
        ])
        self.assertEqual(result["subtask_total"], 2)
        self.assertEqual(result["subtask_status_counts"], {"blocked": 1, "not_started": 1})

    def test_replayed_end_and_controller_projection_count_once(self):
        end = passed("s", "g")
        projected = copy.deepcopy(end)
        projected.update(kind="subtask_end", status="done", subtask="s")
        result = summarize([end, copy.deepcopy(end), projected, copy.deepcopy(projected)])
        self.assertEqual(result["subtask_total"], 1)
        self.assertEqual(result["subtask_completed"], 1)
        self.assertEqual(result["subtask_duration_sum_ms"], 100)

    def test_retry_keeps_one_case_and_old_terminal_replay_cannot_override_new_attempt(self):
        old_end = trace_event("task_end", "g", attempt_id="a1", status="error", duration_ms=50, error="temporary failure")
        events = [trace_event("task_start", "g", attempt_id="a1"), old_end,
                  trace_event("task_start", "g", attempt_id="a2"), passed("g", attempt_id="a2"), copy.deepcopy(old_end)]
        result = summarize(events)
        self.assertEqual((result["goal_total"], result["goal_completed"]), (1, 1))
        self.assertEqual(result["goals"][0]["attempts"], 2)
        self.assertEqual(result["goals"][0]["attempt_id"], "a2")
        self.assertEqual(result["total_duration_ms"], 150)

    def test_unfinished_retry_cannot_reuse_previous_attempt_success(self):
        result = summarize([passed("g", attempt_id="a1"), trace_event("task_start", "g", attempt_id="a2")])
        self.assertEqual(result["completed"], 0)
        self.assertEqual(result["goal_status"], "incomplete")
        self.assertIsNone(result["goals"][0]["duration_ms"])
        self.assertEqual(result["goals"][0]["known_duration_ms"], 100)

    def test_registered_new_attempt_does_not_reuse_prior_terminal_result(self):
        result = summarize([passed("g", attempt_id="a1"),
                            trace_event("task_registered", "g", attempt_id="a2")])
        self.assertEqual(result["completed"], 0)
        self.assertEqual(result["goal_status"], "not_started")
        self.assertEqual(result["goals"][0]["attempt_id"], "a2")

    def test_error_cannot_be_hidden_by_success_for_same_attempt(self):
        events = [trace_event("task_end", "g", status="error", error="fatal crash"), passed("g", kind="orchestration_end")]
        result = summarize(events)
        self.assertEqual(result["completed"], 0)
        self.assertEqual(result["goal_status"], "error")
        self.assertEqual(result["goals"][0]["error"], "conflicting_terminal_events")

    def test_passed_report_cannot_override_failed_execution_status(self):
        event = passed("g")
        event.update(status="error", error="fatal error after evaluation")
        result = summarize([event])
        self.assertEqual(result["completed"], 0)
        self.assertEqual(result["goal_status"], "error")

    def test_report_must_match_task_attempt_and_exact_spec(self):
        for mutate in (
            lambda e: e["acceptance_report"].update(task_id="another-task"),
            lambda e: e["acceptance_report"].update(attempt_id="other-attempt"),
            lambda e: e["acceptance_report"]["spec_ref"].update(version=2),
            lambda e: e["acceptance_report"]["evaluator_ref"].update(id="other-evaluator"),
            lambda e: e["acceptance_report"].update(checks=[]),
            lambda e: e["acceptance_report"]["checks"][0].update(evidence_refs=[]),
            lambda e: e.update(attempt_id=None),
        ):
            event = passed("g")
            mutate(event)
            with self.subTest(event=event):
                result = summarize([event])
                self.assertEqual(result["completed"], 0)
                self.assertEqual(result["goal_status"], "unverified")

    def test_report_must_cover_declared_target_resources(self):
        from miniagent.schemas import RunResult

        event = passed("g")
        event["acceptance_spec"]["target"]["resource_ids"] = ["missing-resource"]
        result = summarize([event])
        self.assertEqual(result["completed"], 0)
        self.assertEqual(result["goals"][0]["error"], "acceptance_target_not_covered")
        run_result = RunResult(task_id="g", attempt_id="a1", agent="fixture", status="completed",
                               **{name: event[name] for name in ("acceptance_spec", "acceptance_report", "acceptance_evidence")})
        self.assertFalse(run_result.succeeded)

    def test_prior_acceptance_event_can_supply_report_but_future_event_cannot(self):
        fields = acceptance_fields("g")
        acceptance = trace_event("acceptance", "g", spec=fields["acceptance_spec"], report=fields["acceptance_report"],
                                 receipts=fields["acceptance_evidence"])
        end = trace_event("task_end", "g", status="completed")
        self.assertEqual(summarize([acceptance, end])["completed"], 1)
        self.assertEqual(summarize([end, acceptance])["completed"], 0)

    def test_old_or_other_attempt_acceptance_does_not_validate_latest_completion(self):
        fields = acceptance_fields("g", "a1")
        acceptance = trace_event("acceptance", "g", attempt_id="a1", **fields)
        end = trace_event("task_end", "g", attempt_id="a2", status="completed")
        self.assertEqual(summarize([acceptance, end])["completed"], 0)

    def test_passed_report_requires_existing_correctly_bound_invocation_receipt(self):
        for change in (
            lambda e: e.pop("acceptance_evidence"),
            lambda e: e.update(acceptance_evidence=[]),
            lambda e: e["acceptance_evidence"][0].update(task_id="other"),
            lambda e: e["acceptance_evidence"][0].update(attempt_id="other"),
            lambda e: e["acceptance_evidence"][0].update(environment_id="other"),
            lambda e: e["acceptance_evidence"][0].update(invoked=False),
            lambda e: e["acceptance_evidence"][0].update(invoked=1),
            lambda e: e["acceptance_evidence"][0].update(outcome="error"),
            lambda e: e["acceptance_evidence"][0].update(results={"C1": [False, "Fixture comparison"]}),
            lambda e: e["acceptance_evidence"][0].update(ended_at="2026-11-17T10:00:00Z"),
        ):
            event = passed("g")
            change(event)
            with self.subTest(event=event):
                result = summarize([event])
                self.assertEqual(result["completed"], 0)
                self.assertEqual(result["goal_status"], "unverified")

    def test_external_evidence_without_resolver_remains_unverified(self):
        for kind in ("artifact", "catalog", "reply"):
            event = passed("g")
            event["acceptance_report"]["checks"][0]["evidence_refs"].append({"kind": kind, "id": "unresolved"})
            with self.subTest(kind=kind):
                self.assertEqual(summarize([event])["completed"], 0)

    def test_receipts_from_another_report_cannot_fill_evidence_gap(self):
        other = acceptance_fields("g")
        other["acceptance_report"]["report_id"] = "old-report"
        event = passed("g")
        event.pop("acceptance_evidence")
        earlier = trace_event("acceptance", "g", **other)
        self.assertEqual(summarize([earlier, event])["completed"], 0)

    def test_terminal_and_event_receipts_may_repeat_identical_evidence(self):
        event = passed("g")
        fields = acceptance_fields("g")
        earlier = trace_event("acceptance", "g", spec=fields["acceptance_spec"], report=fields["acceptance_report"],
                              receipts=fields["acceptance_evidence"])
        self.assertEqual(summarize([earlier, event])["completed"], 1)
        event["acceptance_evidence"][0]["results"]["C1"] = [False, "conflicting copy"]
        self.assertEqual(summarize([earlier, event])["completed"], 0)

    def test_target_hash_requires_matching_file_observation(self):
        event = passed("g")
        digest = "sha256:" + "a" * 64
        event["acceptance_spec"]["target"]["resource_ids"] = ["file-result"]
        event["acceptance_report"]["evaluated_resource_versions"] = [
            {"resource_id": "file-result", "state": "present", "version": digest}]
        reference = {"kind": "receipt", "id": "observed-file"}
        event["acceptance_report"]["evidence_refs"].append(reference)
        event["acceptance_report"]["checks"][0]["evidence_refs"].append(reference)
        event["acceptance_evidence"].append({
            "id": "observed-file", "kind": "file_observation", "environment_id": "fixture-env",
            "resource_id": "file-result", "state": "present", "content_hash": digest, "size_bytes": 7,
            "relative_path": "result.txt", "observed_at": "2026-11-16T10:00:00Z",
        })
        self.assertEqual(summarize([event])["completed"], 1)
        validate_evidence(AcceptanceSpec.model_validate(event["acceptance_spec"]),
                          AcceptanceReport.model_validate(event["acceptance_report"]), event["acceptance_evidence"])
        for change in (
            lambda e: e["acceptance_evidence"].pop(),
            lambda e: e["acceptance_evidence"][-1].update(content_hash="sha256:" + "b" * 64),
            lambda e: e["acceptance_evidence"][-1].update(environment_id="other-env"),
            lambda e: e["acceptance_evidence"][-1].update(observed_at="2026-11-16T09:59:59Z"),
        ):
            bad = copy.deepcopy(event)
            change(bad)
            with self.subTest(bad=bad):
                self.assertEqual(summarize([bad])["completed"], 0)

    def test_real_runtime_file_evaluation_agrees_with_result_and_metrics(self):
        from miniagent.acceptance import accept_files, evaluate
        from miniagent.schemas import RunResult

        directory = make_temp_dir("metrics-file-evaluation")
        self.addCleanup(cleanup, directory)
        (directory / "result.txt").write_text("value=42\n", encoding="utf-8")
        evaluation = evaluate(accept_files("result.txt", min_bytes=1, contains={"result.txt": "value=42"}),
                              directory, task_id="g", attempt_id="a1", objective="produce the exact fixture value")
        payload = evaluation.trace_payload()
        result = RunResult(task_id="g", attempt_id="a1", agent="fixture", status="completed",
                           acceptance_spec=evaluation.spec, acceptance_report=evaluation.report,
                           acceptance_evidence=payload["receipts"])
        self.assertTrue(result.succeeded)
        events = [trace_event("acceptance", "g", **payload),
                  trace_event("task_end", "g", status="completed", acceptance_spec=payload["spec"],
                              acceptance_report=payload["report"])]
        self.assertEqual(summarize(events)["completed"], 1)
        result.acceptance_evidence = []
        self.assertFalse(result.succeeded)

    def test_raw_usage_counts_step_and_planning_once_ignoring_aggregate_totals(self):
        step = trace_event("step", "s", root_id="g", call_id="worker-call", step=1, prompt_tokens=10, completion_tokens=5)
        plan = trace_event("plan_usage", "g", call_id="plan-call", prompt_tokens=4, completion_tokens=3)
        end = passed("g")
        end.update(prompt_tokens=1000, completion_tokens=1000, tokens=99999)
        result = summarize([plan, copy.deepcopy(plan), step, copy.deepcopy(step), end])
        self.assertEqual(result["total_tokens"], 22)
        self.assertEqual(result["goals"][0]["tokens"], 22)
        self.assertEqual(result["subtasks"][0]["tokens"], 15)
        self.assertEqual(result["model_usage_events"], 2)
        self.assertEqual(result["step_count"], 1)

    def test_step_identity_includes_attempt_for_retries(self):
        events = [trace_event("step", "g", attempt_id="a1", step=1, prompt_tokens=10, completion_tokens=1),
                  trace_event("step", "g", attempt_id="a2", step=1, prompt_tokens=20, completion_tokens=2)]
        result = summarize(events + [copy.deepcopy(events[0])])
        self.assertEqual(result["total_tokens"], 33)
        self.assertEqual(result["step_count"], 2)

    def test_missing_usage_is_disclosed_instead_of_implying_zero_consumption(self):
        result = summarize([trace_event("step", "g", step=1, prompt_tokens=10)])
        self.assertEqual(result["total_tokens"], 10)
        self.assertEqual(result["usage_unknown_calls"], 1)

    def test_child_duration_sum_is_separate_from_root_wall_time(self):
        first, second, root = passed("s1", "g"), passed("s2", "g"), passed("g", kind="orchestration_end")
        first["duration_ms"], second["duration_ms"], root["duration_ms"] = 80, 80, 100
        result = summarize([first, second, root])
        self.assertEqual(result["total_duration_ms"], 100)
        self.assertEqual(result["subtask_duration_sum_ms"], 160)
        self.assertIsNone(result["active_duration_ms"])
        self.assertIsNone(result["wait_duration_ms"])

    def test_explicit_active_and_wait_times_are_preserved(self):
        event = passed("g", active_duration_ms=80, wait_duration_ms=20)
        result = summarize([event])
        self.assertEqual(result["active_duration_ms"], 80)
        self.assertEqual(result["wait_duration_ms"], 20)

    def test_fixed_manifest_cannot_be_inflated_by_extra_successes(self):
        result = summarize([passed("extra")], expected_cases=["designated"])
        self.assertEqual((result["total_tasks"], result["completed"]), (1, 0))
        self.assertEqual(result["goals"][0]["status"], "not_started")
        self.assertEqual(result["unexpected_goals"][0]["task_id"], "extra")

    def test_legacy_orchestration_does_not_count_children_as_roots(self):
        events = [
            {"kind": "orchestration_start", "task_id": "g"},
            {"kind": "task_end", "task_id": "s1", "status": "completed"},
            {"kind": "subtask_end", "subtask": "s1", "status": "done"},
            {"kind": "task_end", "level": "goal", "task_id": "g", "status": "completed"},
            {"kind": "orchestration_end", "task_id": "g", "status": "completed"},
        ]
        result = summarize(events)
        self.assertEqual((result["goal_total"], result["subtask_total"]), (1, 1))
        self.assertEqual(result["completed"], 0)
        self.assertEqual(result["subtask_completed"], 0)

    def test_report_lists_every_goal_missing_durations_and_matching_rates(self):
        directory = make_temp_dir("metrics-acceptance")
        self.addCleanup(cleanup, directory)
        events = [passed("one"), trace_event("task_start", "two"), trace_event("case_registered", "three")]
        (directory / "trace.jsonl").write_text("\n".join(json.dumps(event) for event in events) + '\n[]\nnull\n{"kind":', encoding="utf-8")
        result = summarize(load_events(directory))
        report = render_report(directory)
        self.assertEqual(result["goal_total"], 3)
        self.assertIn("33.3%", report)
        self.assertIn("| one |", report)
        self.assertIn("| two | incomplete |", report)
        self.assertIn("| three | not_started |", report)
        self.assertIn("| 活跃时间 | 未记录 |", report)
        self.assertEqual(report, (directory / "report.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
