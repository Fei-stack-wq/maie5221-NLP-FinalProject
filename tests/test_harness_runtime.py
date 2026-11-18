"""Offline receipts for recovery, long tasks, cancellation and input limits."""
from __future__ import annotations

import copy
import json
import threading
import unittest
from unittest.mock import patch

from miniagent.acceptance import accept_files
from miniagent.agent import Agent
from miniagent.config import Settings
from miniagent.metrics import Tracer, load_events
from miniagent.providers import DeepSeekProvider, Provider
from miniagent.rao import Budget
from miniagent.sandbox import Sandbox
from miniagent.schemas import LLMResponse, ToolCall, Usage
from miniagent.tools import build_default_registry
from tests.support import cleanup, make_temp_dir


class RecordingProvider(Provider):
    def __init__(self, responses, after_response=None):
        self.responses = iter(responses)
        self.requests = []
        self.after_response = after_response

    def chat(self, messages, tools=None, temperature=0.0):
        self.requests.append(copy.deepcopy(messages))
        response = next(self.responses)
        if self.after_response:
            self.after_response()
        return response


def write(identity, path, content="working artifact"):
    return ToolCall(id=identity, name="write_file", arguments={"path": path, "content": content})


def response(*calls, finish=None, text=""):
    return LLMResponse(tool_calls=list(calls), text=text,
                       finish_reason=finish or ("tool_calls" if calls else "stop"),
                       usage=Usage(prompt_tokens=10, completion_tokens=5))


class HarnessRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir("harness-runtime")

    def tearDown(self):
        cleanup(self.root)

    def agent(self, provider, tracer=None, settings=None, should_cancel=None):
        return Agent("worker", provider, build_default_registry(),
                     Sandbox(self.root / "workspace", agent_name="worker"),
                     settings or Settings(), tracer=tracer, should_cancel=should_cancel)

    def test_thirteen_valid_tool_rounds_reach_acceptance(self):
        provider = RecordingProvider([response(write(f"write-{i}", f"part-{i}.txt")) for i in range(13)]
                                     + [response(text="done")])
        result = self.agent(provider).run("create thirteen artifacts", acceptance=accept_files("part-12.txt", min_bytes=1))
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(result.steps, 14)
        self.assertEqual(result.tool_calls, 13)
        self.assertEqual(result.usage.total, 14 * 15)

    def test_length_recovery_preserves_successes_and_never_forwards_failed_batch(self):
        provider = RecordingProvider([
            response(write("good", "good.txt", "first version")),
            response(write("bad", "bad.txt"), finish="length"),
            response(write("fixed", "fixed.txt")),
            response(text="done"),
        ])
        snapshots = []
        with Tracer(self.root / "trace") as tracer:
            result = self.agent(provider, tracer).run(
                "write artifacts", acceptance=accept_files("good.txt", "fixed.txt", min_bytes=1),
                on_step=lambda record, state: snapshots.append(copy.deepcopy(state.history)))
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(result.tool_calls, 2)
        self.assertEqual(result.usage.total, 60)
        self.assertFalse((self.root / "workspace" / "bad.txt").exists())
        self.assertEqual((self.root / "workspace" / "good.txt").read_text(), "first version")
        next_request = provider.requests[2]
        ids = [c["id"] for message in next_request for c in message.get("tool_calls", [])]
        replies = [m["tool_call_id"] for m in next_request if m["role"] == "tool"]
        self.assertEqual(ids, ["good"])
        self.assertEqual(replies, ids)
        self.assertTrue(any(m.get("tool_call_id") == "bad" for m in snapshots[-1]))
        events = load_events(self.root / "trace")
        self.assertEqual(sum(e["kind"] == "output_recovery" for e in events), 1)
        self.assertEqual(sum(e["kind"] == "step" for e in events), 4)

    def test_repeated_truncation_is_bounded_and_all_attempts_are_charged(self):
        provider = RecordingProvider([response(write(f"bad-{i}", "bad.txt"), finish="length") for i in range(3)])
        with Tracer(self.root / "trace") as tracer:
            result = self.agent(provider, tracer).run("write", acceptance=accept_files("bad.txt"))
        self.assertEqual(result.status, "error")
        self.assertEqual(len(provider.requests), 3)
        self.assertEqual(result.usage.total, 45)
        self.assertEqual(result.tool_calls, 0)
        self.assertFalse((self.root / "workspace" / "bad.txt").exists())
        self.assertTrue(any(e["kind"] == "output_recovery_exhausted" for e in load_events(self.root / "trace")))

    def test_malformed_length_response_is_not_retried(self):
        provider = RecordingProvider([response(write("duplicate", "a.txt"), write("duplicate", "b.txt"), finish="length")])
        result = self.agent(provider).run("write")
        self.assertEqual(result.status, "error")
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(result.tool_calls, 0)

    def test_output_recovery_can_be_disabled(self):
        provider = RecordingProvider([response(write("bad", "bad.txt"), finish="length")])
        result = self.agent(provider, settings=Settings(max_output_recovery_attempts=0)).run("write")
        self.assertEqual(result.status, "error")
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(result.usage.total, 15)

    def test_recovered_thinking_history_passes_native_payload_validation(self):
        receipts = [response(write("first", "a.txt")),
                    response(write("cut", "cut.txt"), finish="length"),
                    response(write("second", "b.txt")), response(text="done")]
        receipts = [r.model_copy(update={"reasoning_content": f"private-{i}"}) for i, r in enumerate(receipts)]
        provider = RecordingProvider(receipts)
        agent = self.agent(provider)
        result = agent.run("write two files", acceptance=accept_files("a.txt", "b.txt", min_bytes=1))
        self.assertEqual(result.status, "completed", result.error)
        native = DeepSeekProvider("test-only-key", thinking="enabled")
        for messages in provider.requests:
            native._build_payload(messages, agent.unit.tool_specs(), 0.0)
        assistants = [m for m in provider.requests[-1] if m["role"] == "assistant"]
        self.assertEqual([m["reasoning_content"] for m in assistants], ["private-0", "private-2"])
        self.assertEqual([m["tool_calls"][0]["id"] for m in assistants], ["first", "second"])

    def test_protected_input_overflow_stops_before_provider(self):
        provider = RecordingProvider([])
        with Tracer(self.root / "trace") as tracer:
            result = self.agent(provider, tracer, Settings(context_window_tokens=1024, max_output_tokens=512)).run("x" * 2000)
        self.assertEqual(result.status, "limit_exceeded", result.error)
        self.assertIn("T_CONTEXT", result.error)
        self.assertEqual(provider.requests, [])
        self.assertTrue(any(e["kind"] == "context_limit" for e in load_events(self.root / "trace")))

    def test_output_reservation_larger_than_context_reports_actual_limits(self):
        provider = RecordingProvider([])
        result = self.agent(provider, settings=Settings(context_window_tokens=1024)).run("small task")
        self.assertEqual(result.status, "limit_exceeded")
        self.assertEqual(provider.requests, [])
        self.assertIn("context_window_tokens=1024", result.error)
        self.assertIn("max_output_tokens=300000", result.error)
        self.assertIn("input_limit_tokens=0", result.error)

    def test_cancel_before_request_makes_no_provider_call(self):
        provider = RecordingProvider([])
        result = self.agent(provider, should_cancel=lambda: True).run("write")
        self.assertEqual(result.status, "aborted")
        self.assertEqual(result.error, "用户请求停止")
        self.assertEqual(provider.requests, [])

    def test_cancel_after_response_prevents_tools_and_closes_all_ids(self):
        stop = threading.Event()
        provider = RecordingProvider([response(write("a", "a.txt"), write("b", "b.txt"))], after_response=stop.set)
        snapshots = []
        with Tracer(self.root / "trace") as tracer:
            result = self.agent(provider, tracer, should_cancel=stop.is_set).run(
                "write", on_step=lambda record, state: snapshots.append(copy.deepcopy(state.history)))
        self.assertEqual(result.status, "aborted")
        self.assertEqual(result.tool_calls, 0)
        self.assertEqual(result.usage.total, 15)
        self.assertFalse((self.root / "workspace" / "a.txt").exists())
        self.assertEqual([m["tool_call_id"] for m in snapshots[-1] if m["role"] == "tool"], ["a", "b"])
        self.assertTrue(any(e["kind"] == "cancel_observed" for e in load_events(self.root / "trace")))

    def test_cancel_before_acceptance_cannot_mark_task_completed(self):
        stop = threading.Event()
        provider = RecordingProvider([response(text="done")])
        result = self.agent(provider, should_cancel=stop.is_set).run(
            "finish", on_step=lambda record, state: stop.set())
        self.assertEqual(result.status, "aborted")
        self.assertIsNone(result.acceptance_report)

    def test_cancel_between_tools_preserves_first_success_and_skips_remainder(self):
        stop = threading.Event()
        provider = RecordingProvider([response(write("first", "first.txt"), write("second", "second.txt"))])
        agent = self.agent(provider, should_cancel=stop.is_set)
        dispatch = agent.registry.dispatch

        def dispatch_and_cancel(*args, **kwargs):
            result = dispatch(*args, **kwargs)
            stop.set()
            return result

        snapshots = []
        with patch.object(agent.registry, "dispatch", side_effect=dispatch_and_cancel):
            result = agent.run("write", on_step=lambda record, state: snapshots.append(copy.deepcopy(state.history)))
        self.assertEqual(result.status, "aborted")
        self.assertEqual(result.tool_calls, 1)
        self.assertTrue((self.root / "workspace" / "first.txt").exists())
        self.assertFalse((self.root / "workspace" / "second.txt").exists())
        self.assertEqual([m["tool_call_id"] for m in snapshots[-1] if m["role"] == "tool"], ["first", "second"])

    def test_response_over_task_token_budget_does_not_execute_tools(self):
        # Keep the post-response safety net exercised for a provider whose
        # returned usage exceeds our estimate.
        over = response(write("too-late", "a.txt")).model_copy(update={
            "usage": Usage(prompt_tokens=14_000, completion_tokens=10_000)
        })
        provider = RecordingProvider([over])
        snapshots = []
        result = self.agent(provider).run("write", budget=Budget(tokens_left=20_000),
                                         on_step=lambda record, state: snapshots.append(copy.deepcopy(state.history)))
        self.assertEqual(result.status, "limit_exceeded")
        self.assertEqual(result.usage.total, 24_000)
        self.assertEqual(result.tool_calls, 0)
        self.assertFalse((self.root / "workspace" / "a.txt").exists())
        self.assertEqual([m["tool_call_id"] for m in snapshots[-1] if m["role"] == "tool"], ["too-late"])

    def test_unaffordable_input_never_reaches_provider(self):
        provider = RecordingProvider([])
        with Tracer(self.root / "trace") as tracer:
            result = self.agent(provider, tracer).run("user goal " * 4000,
                                                      budget=Budget(tokens_left=8310))
        self.assertEqual(result.status, "limit_exceeded")
        self.assertIn("8310", result.error)
        self.assertEqual(provider.requests, [])
        self.assertEqual(result.usage.total, 0)
        self.assertTrue(any(e["kind"] == "token_budget_preflight" for e in load_events(self.root / "trace")))

    def test_long_project_avoids_replaying_every_prior_artifact_against_task_budget(self):
        # Meter each transmitted message in an explicit deterministic fixture.
        # All 40 steps comfortably fit the physical context window individually.
        class Metered(Provider):
            def __init__(self):
                self.requests = []
                self.full_input = 0
                self.prompt = self.output = 0

            def chat(self, messages, tools=None, temperature=0.0):
                self.requests.append(messages)
                index = len(self.requests)
                result = (response(write(f"part-{index}", f"part-{index}.txt", "x" * 6800))
                          if index < 40 else response(text="done"))
                prompt = 1500 + len(json.dumps(messages, ensure_ascii=False)) // 4
                generated = 1710 if index < 40 else 4
                if index == 1:
                    self.full_input = prompt
                self.prompt += prompt
                self.output += generated
                return result.model_copy(update={"usage": Usage(prompt_tokens=prompt, completion_tokens=generated)})

        provider = Metered()
        def before_compaction(record, state):
            if record.step < 40:
                provider.full_input += 1500 + len(json.dumps(state.history, ensure_ascii=False)) // 4

        with Tracer(self.root / "trace") as tracer:
            result = self.agent(provider, tracer).run("build 39 artifacts", on_step=before_compaction,
                                                       acceptance=accept_files("part-39.txt"))
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(len(provider.requests), 40)
        self.assertEqual(result.usage.total, provider.prompt + provider.output)
        self.assertLess(result.usage.total, 1_000_000)
        self.assertGreater(provider.full_input, 1_000_000)
        self.assertIn("上下文整理", str(provider.requests[-1]))
        for request in provider.requests:
            DeepSeekProvider._validate_history(request, require_reasoning=False)

    def test_repeated_execution_output_with_different_commands_gives_one_progress_prompt(self):
        receipts = [response(ToolCall(id=f"run-{i}", name="run_python",
                              arguments={"code": f"print('same findings' * 20)  # attempt {i}"}))
                    for i in range(4)] + [response(text="done")]
        provider = RecordingProvider(receipts)
        result = self.agent(provider).run("check test findings")
        self.assertEqual(result.status, "unverified", result.error)
        self.assertEqual(sum("测试进展提醒" in str(m.get("content")) for m in provider.requests[-1]), 1)

    def test_slow_provider_exhausting_time_budget_does_not_execute_tools(self):
        # Advance a deterministic clock during the API call; no real sleep.
        clock = [0.0]
        provider = RecordingProvider([response(write("too-late", "a.txt"))],
                                     after_response=lambda: clock.__setitem__(0, 2.0))
        with patch("time.perf_counter", side_effect=lambda: clock[0]):
            result = self.agent(provider).run("write", budget=Budget(seconds_left=1.0))
        self.assertEqual(result.status, "limit_exceeded")
        self.assertEqual(result.tool_calls, 0)
        self.assertEqual(result.usage.total, 15)
        self.assertFalse((self.root / "workspace" / "a.txt").exists())

    def test_context_omission_integration_keeps_running_and_records_estimates(self):
        receipts = [response(write(f"write-{i}", f"part-{i}.txt", "x" * 6000)) for i in range(8)]
        receipts.append(response(text="done"))
        # Exercise the conservative fallback when a provider does not report
        # trustworthy token counts, independently of the measured-usage path.
        receipts = [r.model_copy(update={"usage": Usage(source="unknown")}) for r in receipts]
        provider = RecordingProvider(receipts)
        snapshots = []
        with Tracer(self.root / "trace") as tracer:
            result = self.agent(provider, tracer, Settings(context_window_tokens=32000, max_output_tokens=512)).run(
                "create eight artifacts", acceptance=accept_files("part-7.txt", min_bytes=1),
                on_step=lambda record, state: snapshots.append(copy.deepcopy(state.history)))
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(sum(m["role"] == "assistant" for m in snapshots[-1]), 9)
        events = load_events(self.root / "trace")
        self.assertTrue(any(e["kind"] == "context_compacted" for e in events))
        for event in (e for e in events if e["kind"] == "step_start"):
            self.assertLessEqual(event["estimated_input_tokens"], event["input_limit_tokens"])
        for messages in provider.requests:
            calls = [c["id"] for message in messages for c in message.get("tool_calls", [])]
            replies = [m["tool_call_id"] for m in messages if m["role"] == "tool"]
            self.assertEqual(calls, replies)

    def test_large_ascii_artifact_uses_reported_tokens_instead_of_byte_limit(self):
        large = response(write("large", "large.txt", "a" * 700_000)).model_copy(update={
            "usage": Usage(prompt_tokens=2000, completion_tokens=175_000),
            "reasoning_content": "private reasoning",
        })
        final = response(text="done").model_copy(update={
            "usage": Usage(prompt_tokens=177_100, completion_tokens=20),
            "reasoning_content": "finished",
        })
        provider = RecordingProvider([large, final])
        with Tracer(self.root / "trace") as tracer:
            agent = self.agent(provider, tracer)
            result = agent.run("write a large ASCII artifact", acceptance=accept_files("large.txt", min_bytes=700_000))
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual((self.root / "workspace" / "large.txt").stat().st_size, 700_000)
        self.assertEqual(len(provider.requests), 2)
        second = [e for e in load_events(self.root / "trace") if e["kind"] == "step_start"][1]
        self.assertEqual(second["input_estimate_source"], "provider_usage_anchor")
        self.assertLess(second["estimated_input_tokens"], 180_000)
        DeepSeekProvider("test-only-key", thinking="enabled")._build_payload(
            provider.requests[-1], agent.unit.tool_specs(), 0.0)

    def test_injected_time_budget_is_not_reset_to_full_task_limit(self):
        provider = RecordingProvider([])
        result = self.agent(provider).run("write", budget=Budget(seconds_left=0))
        self.assertEqual(result.status, "limit_exceeded")
        self.assertEqual(provider.requests, [])

    def test_low_budget_warning_is_emitted_once(self):
        provider = RecordingProvider([response(write("a", "a.txt")), response(text="done")])
        with Tracer(self.root / "trace") as tracer:
            result = self.agent(provider, tracer).run("write", acceptance=accept_files("a.txt", min_bytes=1),
                                                     budget=Budget(steps_left=2))
        self.assertEqual(result.status, "completed", result.error)
        warnings = [e for e in load_events(self.root / "trace") if e["kind"] == "budget_warning"]
        self.assertEqual([e["dimension"] for e in warnings], ["steps"])


if __name__ == "__main__":
    unittest.main()
