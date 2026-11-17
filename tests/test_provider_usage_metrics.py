"""Usage provenance and HTTP retries do not become invented zero-cost calls."""
from __future__ import annotations

import json
import unittest

from miniagent.metrics import render_report, summarize
from tests.support import cleanup, make_temp_dir
from tests.test_metrics import trace_event


def usage_event(kind="step", task_id="goal", *, call_id="call-1", transport="http", request_attempts=1, **payload):
    return trace_event(kind, task_id, call_id=call_id, transport=transport,
                       request_attempts=request_attempts, **payload)


class ProviderUsageMetricsTest(unittest.TestCase):
    def test_default_source_preserves_numeric_usage_with_explicit_http_attempt(self):
        result = summarize([usage_event(prompt_tokens=10, completion_tokens=3)])
        self.assertEqual(result["total_tokens"], 13)
        self.assertEqual(result["http_attempts"], 1)
        self.assertEqual(result["usage_unknown_attempts"], 0)
        self.assertEqual(result["usage_unknown_calls"], 0)
        self.assertEqual(result["usage_estimated_calls"], 0)

    def test_offline_calls_never_invent_http_attempts(self):
        cases = [
            ({"usage_source": "reported", "prompt_tokens": 10, "completion_tokens": 3}, 0, 0),
            ({"usage_source": "estimated", "prompt_tokens": 10, "completion_tokens": 3}, 1, 0),
            ({"usage_source": "unknown", "prompt_tokens": 10, "completion_tokens": 3}, 0, 1),
            ({"usage_source": "reported", "prompt_tokens": 10}, 0, 1),
        ]
        for payload, estimated, unknown in cases:
            with self.subTest(payload=payload):
                result = summarize([usage_event(transport="offline", request_attempts=3, **payload)])
                self.assertEqual(result["http_attempts"], 0)
                self.assertEqual(result["usage_unknown_attempts"], 0)
                self.assertEqual(result["http_attempts_unknown_calls"], 0)
                self.assertEqual(result["usage_estimated_calls"], estimated)
                self.assertEqual(result["usage_unknown_calls"], unknown)
                self.assertEqual(result["total_tokens"], payload["prompt_tokens"] + payload.get("completion_tokens", 0))
        failed = summarize([usage_event("provider_error", transport="offline", request_attempts=3)])
        self.assertEqual(failed["http_attempts"], 0)
        self.assertEqual(failed["usage_unknown_attempts"], 0)
        self.assertEqual(failed["usage_unknown_calls"], 1)

    def test_legacy_or_unknown_transport_preserves_tokens_without_claiming_http(self):
        for transport in (None, "unknown", "custom", False):
            for complete in (False, True):
                with self.subTest(transport=transport, complete=complete):
                    event = trace_event("step", "goal", prompt_tokens=10, request_attempts=3)
                    if transport is not None:
                        event["transport"] = transport
                    if complete:
                        event["completion_tokens"] = 3
                    result = summarize([event])
                    self.assertEqual(result["total_tokens"], 13 if complete else 10)
                    self.assertEqual(result["http_attempts"], 0)
                    self.assertEqual(result["usage_unknown_attempts"], 0)
                    self.assertEqual(result["http_attempts_unknown_calls"], 1)
                    self.assertEqual(result["usage_unknown_calls"], 0 if complete else 1)

    def test_http_transport_without_attempt_count_is_unknown(self):
        event = usage_event(prompt_tokens=10, completion_tokens=3)
        del event["request_attempts"]
        result = summarize([event])
        self.assertEqual(result["total_tokens"], 13)
        self.assertEqual(result["http_attempts"], 0)
        self.assertEqual(result["usage_unknown_attempts"], 0)
        self.assertEqual(result["http_attempts_unknown_calls"], 1)
        self.assertEqual(result["usage_unknown_calls"], 1)

    def test_reported_zero_and_unknown_zero_are_distinct(self):
        for source, unknown in (("reported", 0), ("unknown", 1)):
            with self.subTest(source=source):
                result = summarize([usage_event(prompt_tokens=0, completion_tokens=0,
                                                usage_source=source, request_attempts=1)])
                self.assertEqual(result["total_tokens"], 0)
                self.assertEqual(result["usage_unknown_calls"], unknown)
                self.assertEqual(result["usage_unknown_attempts"], unknown)

    def test_estimated_usage_is_preserved_and_disclosed_separately(self):
        result = summarize([usage_event(prompt_tokens=12, completion_tokens=5, usage_source="estimated")])
        self.assertEqual(result["total_tokens"], 17)
        self.assertEqual(result["usage_estimated_calls"], 1)
        self.assertEqual(result["usage_unknown_calls"], 0)
        self.assertEqual(result["usage_unknown_attempts"], 0)

    def test_successful_request_keeps_unknown_cost_of_failed_http_attempts(self):
        result = summarize([usage_event(prompt_tokens=10, completion_tokens=3,
                                        usage_source="reported", request_attempts=3)])
        self.assertEqual(result["total_tokens"], 13)
        self.assertEqual(result["http_attempts"], 3)
        self.assertEqual(result["usage_unknown_attempts"], 2)
        self.assertEqual(result["usage_unknown_calls"], 1)

    def test_failed_provider_request_counts_all_attempts_without_invented_tokens(self):
        result = summarize([usage_event("provider_error", usage_source="unknown", request_attempts=3,
                                        prompt_tokens=0, completion_tokens=0)])
        self.assertEqual(result["total_tokens"], 0)
        self.assertEqual(result["http_attempts"], 3)
        self.assertEqual(result["usage_unknown_attempts"], 3)
        self.assertEqual(result["usage_unknown_calls"], 1)
        self.assertEqual(result["model_usage_events"], 1)
        self.assertEqual(result["step_count"], 0)
        self.assertEqual(result["total_tasks"], 1)
        self.assertEqual(result["goals"][0]["status"], "incomplete")

    def test_pre_admission_failure_has_zero_http_attempts_without_unknown_charge(self):
        result = summarize([usage_event("provider_error", request_attempts=0, usage_source="unknown")])
        self.assertEqual(result["http_attempts"], 0)
        self.assertEqual(result["usage_unknown_attempts"], 0)
        self.assertEqual(result["usage_unknown_calls"], 0)
        self.assertEqual(result["model_usage_events"], 1)

    def test_partial_numbers_are_kept_but_not_mistaken_for_complete_usage(self):
        result = summarize([usage_event(prompt_tokens=11, usage_source="reported", request_attempts=2)])
        self.assertEqual(result["total_tokens"], 11)
        self.assertEqual(result["usage_unknown_calls"], 1)
        self.assertEqual(result["usage_unknown_attempts"], 2)

    def test_unknown_source_can_still_carry_a_known_partial_number(self):
        result = summarize([usage_event(prompt_tokens=11, completion_tokens=0,
                                        usage_source="unknown", request_attempts=2)])
        self.assertEqual(result["total_tokens"], 11)
        self.assertEqual(result["usage_unknown_attempts"], 2)

    def test_nested_usage_source_is_supported_and_explicit_event_source_wins(self):
        nested = {"prompt_tokens": 7, "completion_tokens": 2, "source": "estimated"}
        result = summarize([usage_event(usage=nested)])
        self.assertEqual(result["total_tokens"], 9)
        self.assertEqual(result["usage_estimated_calls"], 1)
        result = summarize([usage_event(usage=nested, usage_source="unknown")])
        self.assertEqual(result["usage_estimated_calls"], 0)
        self.assertEqual(result["usage_unknown_calls"], 1)

    def test_provider_error_cannot_self_declare_reported_zero(self):
        result = summarize([usage_event("provider_error", usage_source="reported", request_attempts=2,
                                        prompt_tokens=0, completion_tokens=0)])
        self.assertEqual(result["usage_unknown_attempts"], 2)
        self.assertEqual(result["usage_unknown_calls"], 1)

    def test_replayed_provider_errors_and_terminal_aggregates_are_not_double_counted(self):
        failure = usage_event("provider_error", request_attempts=2, usage_source="unknown")
        end = trace_event("task_end", "goal", status="error", tokens=99999,
                          prompt_tokens=99999, completion_tokens=99999, request_attempts=99)
        result = summarize([failure, dict(failure), end])
        self.assertEqual(result["model_usage_events"], 1)
        self.assertEqual(result["http_attempts"], 2)
        self.assertEqual(result["usage_unknown_attempts"], 2)
        self.assertEqual(result["total_tokens"], 0)

    def test_later_finalized_receipt_replaces_same_call_estimate(self):
        estimated = usage_event(prompt_tokens=20, completion_tokens=10, usage_source="estimated", request_attempts=1)
        reported = dict(estimated, prompt_tokens=9, completion_tokens=4, usage_source="reported", request_attempts=2)
        result = summarize([estimated, reported])
        self.assertEqual(result["total_tokens"], 13)
        self.assertEqual(result["model_usage_events"], 1)
        self.assertEqual(result["usage_estimated_calls"], 0)
        self.assertEqual(result["http_attempts"], 2)
        self.assertEqual(result["usage_unknown_attempts"], 1)

    def test_same_call_id_in_separate_logical_attempts_is_not_deduplicated(self):
        events = [usage_event("provider_error", request_attempts=2, attempt_id="a1"),
                  usage_event("provider_error", request_attempts=1, attempt_id="a2")]
        result = summarize(events)
        self.assertEqual(result["model_usage_events"], 2)
        self.assertEqual(result["http_attempts"], 3)
        self.assertEqual(result["usage_unknown_attempts"], 3)
        self.assertEqual(result["usage_unknown_calls"], 2)
        self.assertEqual(result["total_tasks"], 1)

    def test_mixed_planner_child_and_failure_usage_matches_manual_totals(self):
        events = [
            usage_event(task_id="goal:child", root_id="goal", call_id="worker", prompt_tokens=10,
                        completion_tokens=5, usage_source="reported", request_attempts=3),
            usage_event("plan_usage", call_id="planner", prompt_tokens=4, completion_tokens=3,
                        usage_source="estimated", request_attempts=2),
            usage_event(call_id="unknown", prompt_tokens=0, completion_tokens=0, usage_source="unknown"),
            usage_event("provider_error", task_id="goal:child", root_id="goal", call_id="failed", request_attempts=2),
            usage_event(call_id="legacy", prompt_tokens=2, completion_tokens=1),
            usage_event(call_id="partial", prompt_tokens=5),
        ]
        result = summarize(events)
        self.assertEqual(result["total_tokens"], 30)
        self.assertEqual(result["http_attempts"], 10)
        self.assertEqual(result["usage_unknown_attempts"], 7)
        self.assertEqual(result["usage_unknown_calls"], 5)
        self.assertEqual(result["usage_estimated_calls"], 1)
        self.assertEqual(result["model_usage_events"], 6)
        self.assertEqual(result["step_count"], 4)
        self.assertEqual(result["goals"][0]["tokens"], 30)
        self.assertEqual(result["subtasks"][0]["tokens"], 15)

    def test_invalid_metadata_does_not_claim_known_zero_or_count_boolean_tokens(self):
        for source in (None, "unrecognized", [], {}):
            with self.subTest(source=source):
                result = summarize([usage_event(usage_source=source, prompt_tokens=True, completion_tokens=5)])
                self.assertEqual(result["total_tokens"], 5)
                self.assertEqual(result["usage_unknown_calls"], 1)
        for attempts in (0, -1, True, "2", None):
            with self.subTest(attempts=attempts):
                result = summarize([usage_event(request_attempts=attempts, prompt_tokens=2, completion_tokens=3)])
                self.assertEqual(result["total_tokens"], 5)
                self.assertEqual(result["http_attempts"], 0)
                self.assertEqual(result["http_attempts_unknown_calls"], 1)
                self.assertEqual(result["usage_unknown_calls"], 1)

    def test_report_discloses_estimation_unknown_attempts_and_partial_total(self):
        directory = make_temp_dir("usage-report")
        self.addCleanup(cleanup, directory)
        events = [usage_event(prompt_tokens=6, completion_tokens=3, usage_source="estimated", request_attempts=2),
                  usage_event("provider_error", call_id="failed", request_attempts=3)]
        (directory / "trace.jsonl").write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
        report = render_report(directory, write=False)
        self.assertIn("已记录 token（含估算/已知部分） | 9", report)
        self.assertIn("使用估算用量的调用数 | 1", report)
        self.assertIn("已记录 HTTP 尝试数 | 5", report)
        self.assertIn("用量未知的 HTTP 尝试数 | 4", report)
        self.assertIn("未知不等于零", report)
        self.assertIn("不是完整账单或预算 ledger", report)


if __name__ == "__main__":
    unittest.main()
