"""The compaction call is model-backed, usage-bearing, and has no execution tools."""
import copy
import json
import unittest
from unittest.mock import patch

from miniagent.compaction import RollingCompactor
from miniagent.context_window import project_history
from miniagent.provider_http import ProviderError
from miniagent.providers import DeepSeekProvider
from miniagent.schemas import LLMResponse, Usage


def add_read(history, number):
    call_id = f"reading-{number}"
    history += [
        {"role": "assistant", "content": None, "reasoning_content": "private thoughts",
         "tool_calls": [{"id": call_id, "type": "function", "function": {
             "name": "read_file", "arguments": json.dumps({"path": f"file-{number}.py"})}}]},
        {"role": "tool", "tool_call_id": call_id, "content": f"source-{number}: " + "x" * 2200},
    ]


def history():
    rows = [{"role": "system", "content": "trusted system"}, {"role": "user", "content": "fix booking"}]
    for i in range(8):
        add_read(rows, i)
    return rows


def projection(rows):
    return project_history(rows, tool_schemas=[], context_window_tokens=60_000, output_reserve=1000,
                           replay_input_target=5000)


def summary(check="Booking overlap test passed in fixture A"):
    return LLMResponse(text=json.dumps({"goals": ["fix booking"], "decisions": [], "files": ["file-0.py"],
                                        "checks": [check], "blockers": []}),
                       usage=Usage(prompt_tokens=250, completion_tokens=85), finish_reason="stop",
                       request_attempts=2)


class Capture:
    def __init__(self, response=None):
        self.requests = []
        self.response = response or summary()

    def summarize_context(self, messages, *, max_output_tokens, deadline_seconds):
        self.requests.append((messages, max_output_tokens, deadline_seconds))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def compact(compactor, rows, source, **kwargs):
    options = {"available_tokens": None, "seconds_left": 100, "context_window_tokens": 60_000, "force": True}
    return compactor.update(rows, projection(rows), source, **{**options, **kwargs})


class SemanticCompactionTest(unittest.TestCase):
    def test_rolls_over_only_new_batch_and_keeps_recent_tool_pairs_and_user_message(self):
        raw, compactor, source = history(), RollingCompactor(), Capture()
        raw.insert(6, {"role": "user", "content": "Correct the target to A101."})
        original = copy.deepcopy(raw)
        first = compact(compactor, raw, source)
        self.assertEqual((first.status, first.usage.total, first.request_attempts), ("applied", 335, 2))
        self.assertEqual(original, raw)
        self.assertEqual(first.covered_groups, 6)
        self.assertIn("Booking overlap test passed", json.dumps(first.projection.messages))
        self.assertIn(raw[6], first.projection.messages)
        self.assertEqual(first.projection.messages[-4:], raw[-4:])
        DeepSeekProvider._validate_history(first.projection.messages, require_reasoning=True)
        evidence = json.dumps(source.requests[0][0])
        self.assertIn("file-0.py", evidence)
        self.assertNotIn("private thoughts", evidence)
        self.assertNotIn("file-6.py", evidence)
        again = compact(compactor, raw, source)
        self.assertEqual((again.status, again.usage.total, len(source.requests)), ("cached", 0, 1))
        add_read(raw, 8)
        next_pass = compact(compactor, raw, source)
        self.assertEqual(next_pass.new_groups, 1)
        data = json.loads(source.requests[-1][0][1]["content"])
        self.assertEqual(len(data["earlier_batches"]), 1)
        self.assertIn("file-6.py", str(data["earlier_batches"]))
        self.assertNotIn("file-0.py", str(data["earlier_batches"]))
        self.assertEqual(data["previous_checkpoint"]["checks"], ["Booking overlap test passed in fixture A"])

    def test_no_partial_summary_or_double_billing_after_failure(self):
        rows, state, source = history(), RollingCompactor(), Capture()
        compact(state, rows, source)
        for i in range(8, 11):
            add_read(rows, i)
        source.response = summary().model_copy(update={"finish_reason": "length"})
        result = compact(state, rows, source)
        self.assertEqual(result.status, "invalid_summary")
        self.assertEqual(result.usage.total, 335)
        self.assertEqual(result.covered_groups, 6)
        self.assertIn("6/9", json.dumps(result.projection.messages))
        cooled = compact(state, rows, source)
        self.assertEqual((cooled.status, cooled.attempted, cooled.usage.total), ("cooldown", False, 0))

    def test_real_provider_path_disables_tools_and_thinking_with_separate_usage(self):
        rows = history()
        provider = DeepSeekProvider("test-only", thinking="enabled", max_output_tokens=2500)
        calls = []
        def capture(instance, messages, **kwargs):
            calls.append((instance.thinking, kwargs))
            return summary()
        with patch.object(DeepSeekProvider, "chat", capture):
            result = compact(RollingCompactor(), rows, provider)
        self.assertEqual(provider.thinking, "enabled")
        self.assertEqual(result.status, "applied")
        mode, opts = calls[0]
        self.assertEqual(mode, "disabled")
        self.assertIsNone(opts["tools"])
        self.assertLessEqual(opts["max_output_tokens"], 2500)
        self.assertLessEqual(opts["deadline_seconds"], 50)

    def test_finite_budget_or_timeout_cannot_authorize_unaffordable_summary(self):
        source = Capture()
        rows = history()
        for opts in ({"available_tokens": projection(rows).estimated_input_tokens + 900},
                     {"seconds_left": 1}):
            result = compact(RollingCompactor(), rows, source, **opts)
            self.assertEqual(result.status, "insufficient_budget")
            self.assertFalse(result.attempted)
        self.assertEqual(source.requests, [])

    def test_failed_provider_and_unknown_usage_leave_bill_and_full_replay_trace_separate(self):
        for source, expected_usage, state in [
                (Capture(ProviderError("rejected", status_code=400, attempts=1)), "unknown", "provider_failed"),
                (Capture(TimeoutError()), "estimated", "provider_failed"),
                (Capture(summary().model_copy(update={"usage": Usage(source="unknown")})), "estimated", "applied"),
                (Capture(LLMResponse(text="invented plain answer", finish_reason="stop")), "estimated", "invalid_summary")]:
            with self.subTest(state=state, usage=expected_usage):
                rows = history()
                result = compact(RollingCompactor(), rows, source)
                self.assertEqual((result.status, result.usage.source), (state, expected_usage))
                if expected_usage == "estimated":
                    self.assertGreater(result.usage.total, 0)
                if state != "applied":
                    self.assertEqual(result.projection, projection(rows))
                    self.assertEqual(result.covered_groups, 0)

    def test_old_checkpoints_never_leak_into_new_history_or_protected_human_batch(self):
        raw, state, source = history(), RollingCompactor(), Capture()
        compact(state, raw, source)
        other = history()
        other[2]["tool_calls"][0]["function"] = {"name": "ask_human", "arguments": "{}"}
        other[3]["content"] = "author:human unique reply"
        source.response = summary("fresh feedback")
        result = compact(state, other, source)
        new_input = json.loads(source.requests[-1][0][1]["content"])
        self.assertIsNone(new_input["previous_checkpoint"])
        self.assertNotIn("author:human unique reply", str(new_input))
        self.assertIn(other[3], result.projection.messages)


if __name__ == "__main__":
    unittest.main()
