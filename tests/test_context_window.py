"""Whole-batch context omission and protected input fail-closed boundaries."""
from __future__ import annotations

import copy
import unittest

from miniagent.context_window import ContextLimitError, UsageAnchor, project_history
from miniagent.providers import DeepSeekProvider


def batch(number: int, size: int = 1500):
    return [
        {"role": "assistant", "content": None, "reasoning_content": f"private-reason-{number}", "tool_calls": [
            {"id": f"call-{number}", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'}}]},
        {"role": "tool", "tool_call_id": f"call-{number}", "content": "x" * size},
    ]


class ContextWindowTest(unittest.TestCase):
    def test_omits_whole_old_batches_without_mutating_raw_history(self):
        history = [{"role": "system", "content": "system instructions"},
                   {"role": "user", "content": "original goal"}]
        for index in range(8):
            history.extend(batch(index))
            if index == 3:
                history.append({"role": "user", "content": "critical later user correction"})
        original = copy.deepcopy(history)
        projection = project_history(history, tool_schemas=[], context_window_tokens=7000, output_reserve=500)
        self.assertEqual(history, original)
        self.assertGreater(projection.omitted_groups, 0)
        self.assertLessEqual(projection.estimated_input_tokens, projection.input_limit)
        self.assertIn(history[0], projection.messages)
        self.assertIn(history[1], projection.messages)
        self.assertTrue(any(m.get("content") == "critical later user correction" for m in projection.messages))
        for message in batch(6) + batch(7):
            self.assertIn(message, projection.messages)
        calls = [m["tool_calls"][0]["id"] for m in projection.messages if m.get("tool_calls")]
        replies = [m["tool_call_id"] for m in projection.messages if m["role"] == "tool"]
        self.assertEqual(calls, replies)
        self.assertTrue(any("不是内容摘要" in str(m.get("content")) for m in projection.messages))
        DeepSeekProvider._validate_history(projection.messages, require_reasoning=True)
        for index in (6, 7):
            assistant = next(m for m in projection.messages
                             if m.get("tool_calls", [{}])[0].get("id") == f"call-{index}")
            self.assertEqual(assistant["reasoning_content"], f"private-reason-{index}")

    def test_replay_target_preserves_actual_human_answer_and_recent_actions(self):
        human = batch(0, 12)
        human[0]["tool_calls"][0]["function"]["name"] = "ask_human"
        human[1]["content"] = "Use the two-year horizon agreed with the requester"
        history = [{"role": "user", "content": "write report"}] + human
        for i in range(1, 10):
            history.extend(batch(i))
        trimmed = project_history(history, tool_schemas=[], context_window_tokens=1_000_000,
                                  output_reserve=300_000, task_tokens_left=45_000, steps_left=40)
        self.assertGreater(trimmed.omitted_groups, 0)
        for message in human + batch(8) + batch(9):
            self.assertIn(message, trimmed.messages)
        self.assertGreater(trimmed.estimated_input_tokens, 45_000 // 40)

    def test_output_reservation_and_tool_schemas_are_counted(self):
        history = [{"role": "user", "content": "hello"}]
        small = project_history(history, tool_schemas=[], context_window_tokens=10000, output_reserve=100)
        schema = project_history(history, tool_schemas=[{"description": "x" * 1000}],
                                 context_window_tokens=10000, output_reserve=200)
        self.assertGreater(schema.estimated_input_tokens, small.estimated_input_tokens + 1000)
        self.assertEqual(small.input_limit - schema.input_limit, 100)
        with self.assertRaises(ContextLimitError):
            project_history(history, tool_schemas=[], context_window_tokens=1000, output_reserve=1000)

    def test_oversized_protected_message_and_incomplete_batch_fail_closed(self):
        with self.assertRaises(ContextLimitError):
            project_history([{"role": "user", "content": "x" * 10000}], tool_schemas=[],
                            context_window_tokens=2000, output_reserve=100)
        for messages in [batch(1)[:1], batch(1)[1:], [batch(1)[0], batch(2)[1]]]:
            with self.subTest(messages=messages), self.assertRaises(ContextLimitError):
                project_history(messages, tool_schemas=[], context_window_tokens=10000, output_reserve=100)

    def test_failed_batch_can_be_excluded_without_orphan_results(self):
        history = [{"role": "user", "content": "goal"}] + batch(0) + batch(1) + batch(2)
        projection = project_history(history, tool_schemas=[], context_window_tokens=10000,
                                     output_reserve=100, excluded_ranges=[(3, 5)])
        self.assertEqual([m["tool_call_id"] for m in projection.messages if m["role"] == "tool"],
                         ["call-0", "call-2"])
        self.assertEqual(len(history), 7)

    def test_measured_completion_survives_anchor_prefix_change(self):
        assistant = {"role": "assistant", "content": "a" * 700_000, "reasoning_content": "private"}
        original = [{"role": "user", "content": "initial"}, assistant]
        anchor = UsageAnchor(copy.deepcopy(original), [], 177_000)
        history = [{"role": "user", "content": "changed prefix"}, assistant,
                   {"role": "user", "content": "continue"}]
        projection = project_history(
            history, tool_schemas=[], context_window_tokens=1_000_000, output_reserve=300_000,
            usage_anchor=anchor, reported_assistant_tokens={1: (copy.deepcopy(assistant), 175_000)},
        )
        self.assertEqual(projection.estimate_source, "reported_completion_and_bytes")
        self.assertLess(projection.estimated_input_tokens, 180_000)
        # No reported counts means no fabricated density calibration.
        with self.assertRaises(ContextLimitError):
            project_history(history, tool_schemas=[], context_window_tokens=1_000_000,
                            output_reserve=300_000, usage_anchor=anchor)

    def test_anchor_and_message_counts_are_bound_to_exact_snapshot_and_schemas(self):
        assistant = {"role": "assistant", "content": "a" * 700_000}
        history = [{"role": "user", "content": "initial"}, assistant]
        anchor = UsageAnchor(copy.deepcopy(history), [], 177_000)
        projection = project_history(history, tool_schemas=[], context_window_tokens=1_000_000,
                                     output_reserve=300_000, usage_anchor=anchor)
        self.assertEqual(projection.estimate_source, "provider_usage_anchor")
        with self.assertRaises(ContextLimitError):
            project_history(history, tool_schemas=[{"description": "changed"}],
                            context_window_tokens=1_000_000, output_reserve=300_000, usage_anchor=anchor)
        mismatched_snapshot = {**assistant, "content": "not the same response"}
        with self.assertRaises(ContextLimitError):
            project_history(history, tool_schemas=[], context_window_tokens=1_000_000,
                            output_reserve=300_000,
                            reported_assistant_tokens={1: (mismatched_snapshot, 175_000)})


if __name__ == "__main__":
    unittest.main()
