"""Protocol receipts through the real adapter, RAO, runtime trace and CLI."""
from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch

from miniagent.agent import Agent
from miniagent.acceptance import accept_files
from miniagent.cli import main, _make_provider_factory
from miniagent.config import Settings
from miniagent.metrics import Tracer, load_events, summarize
from miniagent.orchestrator import Orchestrator
from miniagent.provider_http import HTTPResult, ProviderError
from miniagent.providers import DeepSeekProvider, ScriptedProvider
from miniagent.sandbox import Sandbox
from miniagent.tools import build_default_registry
from tests.support import cleanup, make_temp_dir
from tests.test_providers import envelope


class ProviderRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir("provider-runtime")

    def tearDown(self):
        cleanup(self.root)

    def agent(self, provider, tracer):
        return Agent(name="worker", provider=provider, registry=build_default_registry(),
                     sandbox=Sandbox(self.root / "agent", agent_name="worker"),
                     settings=Settings(), tracer=tracer)

    @patch("miniagent.providers.post_json")
    def test_recovery_tool_choice_respects_live_mode_and_disclosed_tools(self, post):
        from miniagent.rao import StepPolicy
        from miniagent.agent import COORDINATOR_PROMPT
        for mode in ("disabled", "enabled"):
            with self.subTest(mode=mode):
                post.return_value = HTTPResult(envelope(content="test summary"), 1)
                runner = self.agent(DeepSeekProvider("test-key", thinking=mode), None)
                policy = StepPolicy("coordinate", allowed_tools=frozenset({"delegate_task"}), required_tool="delegate_task")
                runner.run("prepare routing", system_prompt=COORDINATOR_PROMPT, step_policy=lambda *_: policy)
                payload = post.call_args.args[1]
                self.assertEqual(payload["tool_choice"], {"type": "function", "function": {"name":"delegate_task"}}
                                 if mode == "disabled" else "auto")
                self.assertEqual(payload["messages"][0]["content"], COORDINATOR_PROMPT)
        with self.assertRaises(ValueError):
            StepPolicy("handoff", allowed_tools=frozenset(), required_tool="write_file")

    @patch("miniagent.providers.post_json")
    def test_two_tool_rounds_thinking_receipts_and_trace_privacy(self, post):
        calls = [
            {"id": "write-1", "type": "function", "function": {
                "name": "write_file", "arguments": ' {"path":"proof.md", "content":"proof-value"} '}},
            {"id": "read-1", "type": "function", "function": {
                "name": "read_file", "arguments": '{"path":"proof.md"}'}},
        ]
        receipts = [envelope(calls=[call], finish="tool_calls", reasoning=f"hidden-{i}", content=None)
                    for i, call in enumerate(calls)]
        receipts.append(envelope(content="done", reasoning="hidden-final"))
        post.side_effect = [HTTPResult(data, attempts=2 if i == 0 else 1, latency_ms=1)
                            for i, data in enumerate(receipts)]
        with Tracer(self.root / "trace") as tracer:
            result = self.agent(DeepSeekProvider("test-key", thinking="enabled"), tracer).run(
                "write and verify", acceptance=accept_files("proof.md", min_bytes=1))
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(result.tool_calls, 2)
        self.assertEqual(result.usage.total, 45)
        self.assertEqual(result.usage.source, "unknown")
        history = post.call_args_list[2].args[1]["messages"]
        assistants = [m for m in history if m["role"] == "assistant"]
        self.assertEqual([m["reasoning_content"] for m in assistants], ["hidden-0", "hidden-1"])
        self.assertEqual(assistants[0]["tool_calls"][0]["function"]["arguments"], calls[0]["function"]["arguments"])
        tools = [m for m in history if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tools], ["write-1", "read-1"])
        self.assertIn("proof-value", tools[1]["content"])
        trace = (self.root / "trace" / "trace.jsonl").read_text()
        self.assertNotIn("hidden-", trace)
        self.assertNotIn("test-key", trace)
        steps = [e for e in load_events(self.root / "trace") if e["kind"] == "step"]
        self.assertEqual(steps[0]["request_attempts"], 2)
        self.assertEqual(steps[0]["usage_source"], "reported")
        self.assertEqual(steps[-1]["finish_reason"], "stop")

    @patch("miniagent.providers.post_json")
    def test_large_tool_response_can_continue_to_acceptance_with_default_child_budget(self, post):
        # Reported usage models a large output without generating or paying for it.
        # The old 160k child budget stopped immediately after the first tool call.
        from miniagent.rao import Budget
        call = {"id": "large-write", "type": "function", "function": {
            "name": "write_file", "arguments": '{"path":"proof.md","content":"verified output"}'}}
        written = envelope(calls=[call], finish="tool_calls")
        written["usage"] = {"prompt_tokens": 100, "completion_tokens": 300000,
                            "total_tokens": 300100}
        final = envelope(content="done")
        final["usage"] = {"prompt_tokens": 300200, "completion_tokens": 20,
                          "total_tokens": 300220}
        post.side_effect = [HTTPResult(written, attempts=1), HTTPResult(final, attempts=1)]
        with Tracer(self.root / "trace") as tracer:
            result = self.agent(DeepSeekProvider("test-key"), tracer).run(
                "write and verify", acceptance=accept_files("proof.md", min_bytes=1),
                budget=Budget.from_limits(Settings().limits, factor=0.8))
        self.assertEqual(result.status, "completed", result.error)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(result.usage.total, 600320)
        allocations = [call.args[1]["max_tokens"] for call in post.call_args_list]
        self.assertEqual(allocations[0], 300000)
        starts = [event for event in load_events(self.root / "trace") if event["kind"] == "step_start"]
        self.assertEqual(allocations[1], starts[1]["tokens_remaining"] - starts[1]["estimated_input_tokens"])
        self.assertLess(0, allocations[1])
        self.assertLess(allocations[1], 300000)
        self.assertEqual(starts[1]["provider_config"]["max_output_tokens"], 300000)

    @patch("miniagent.providers.post_json")
    def test_truncated_response_never_executes_or_passes_acceptance(self, post):
        call = {"id": "c1", "type": "function", "function": {"name": "write_file",
                "arguments": '{"path":"bad.md","content":"must not write"}'}}
        post.return_value = HTTPResult(envelope(calls=[call], finish="length"), attempts=1, latency_ms=1)
        with Tracer(self.root / "trace") as tracer:
            result = self.agent(DeepSeekProvider("test-key"), tracer).run("truncated")
        self.assertEqual(result.status, "error")
        self.assertEqual(result.tool_calls, 0)
        self.assertEqual(result.usage.total, 45)
        self.assertEqual(post.call_count, 3)
        self.assertFalse((self.root / "agent" / "bad.md").exists())
        self.assertNotIn("acceptance", [e["kind"] for e in load_events(self.root / "trace")])
        self.assertIn("max_output_tokens=300000", result.error)
        self.assertIn("finish_reason=length", result.error)
        self.assertIn("本轮工具未执行", result.error)
        step = next(e for e in load_events(self.root / "trace") if e["kind"] == "step")
        self.assertEqual(step["max_output_tokens"], 300_000)

    @patch("miniagent.providers.post_json")
    def test_in_flight_step_is_observable_before_http_returns_without_private_data(self, post):
        def complete(*_args, **_kwargs):
            events = load_events(self.root / "trace")
            start = events[-1]
            self.assertEqual(start["kind"], "step_start")
            self.assertEqual(start["step"], 1)
            self.assertEqual(start["provider_config"]["max_output_tokens"], 1234)
            self.assertGreater(start["seconds_remaining"], 0)
            self.assertFalse(any(e["kind"] == "step" for e in events))
            return HTTPResult(envelope(content="done", reasoning="private-reasoning"), attempts=1)

        post.side_effect = complete
        with Tracer(self.root / "trace") as tracer:
            self.agent(DeepSeekProvider("private-key", max_output_tokens=1234), tracer).run("work")
        events = load_events(self.root / "trace")
        trace = (self.root / "trace" / "trace.jsonl").read_text()
        for private in ("private-key", "private-reasoning", "https://api.deepseek.com"):
            self.assertNotIn(private, trace)
        self.assertTrue(all(e["iso"].endswith("Z") for e in events))

    @patch("miniagent.providers.post_json")
    def test_truncated_worker_error_is_not_masked_by_missing_root_acceptance(self, post):
        plan = {"subtasks": [
            {"id": "s1", "objective": "write design", "assignee": "worker", "deps": []},
            {"id": "s2", "objective": "write game", "assignee": "worker", "deps": ["s1"]},
        ]}
        call = {"id": "write", "type": "function", "function": {
            "name": "write_file", "arguments": '{"path":"design.md","content":"partial"}'}}
        for scheduler in ("serial", "parallel", "evented"):
            with self.subTest(scheduler=scheduler):
                post.side_effect = [
                    HTTPResult(envelope(content=json.dumps(plan)), attempts=1),
                    *[HTTPResult(envelope(calls=[call], finish="length"), attempts=1) for _ in range(3)],
                ]
                directory = self.root / scheduler
                orchestrator = Orchestrator(
                    provider=DeepSeekProvider("private-key"), settings=Settings(), run_dir=directory,
                )
                result = orchestrator.run("make a web game", scheduler=scheduler)
                self.assertEqual(result.status, "error")
                self.assertEqual(result.acceptance_report.verdict, "unverified")
                self.assertEqual([t.status for t in result.subtasks], ["error", "blocked"])
                self.assertIn("s1", result.error)
                self.assertIn("finish_reason=length", result.error)
                self.assertEqual(orchestrator.task_service.store.get(result.task_id).status, "failed")
                self.assertFalse(list(directory.rglob("design.md")))
                events = load_events(directory)
                self.assertEqual(events[-1]["execution_error"], result.error)
                start = next(e for e in events if e["kind"] == "plan_start")
                end = next(e for e in events if e["kind"] == "plan_usage")
                self.assertEqual(start["call_id"], end["call_id"])
                self.assertEqual(end["max_output_tokens"], 300_000)
                self.assertGreaterEqual(end["latency_ms"], 0)

    @patch("miniagent.providers.post_json")
    def test_failed_http_attempts_have_trace_and_unknown_usage(self, post):
        post.side_effect = ProviderError("Request failed", code="http_error", status_code=503, attempts=3)
        with Tracer(self.root / "trace") as tracer:
            result = self.agent(DeepSeekProvider("test-key"), tracer).run("request fails")
        self.assertEqual(result.status, "error")
        self.assertEqual(result.usage.source, "unknown")
        events = load_events(self.root / "trace")
        error = next(e for e in events if e["kind"] == "provider_error")
        self.assertEqual(error["request_attempts"], 3)
        self.assertEqual(error["http_status"], 503)
        self.assertGreaterEqual(error["latency_ms"], 0)
        self.assertEqual(error["step"], 1)
        self.assertFalse(error["retryable"])
        summary = summarize(events)
        self.assertEqual(summary["goal_completed"], 0)

    @patch("miniagent.providers.post_json")
    def test_planner_does_not_dispatch_truncated_valid_json(self, post):
        plan = {"subtasks": [{"id": "s1", "objective": "work", "assignee": "worker", "deps": []}]}
        post.return_value = HTTPResult(envelope(content=json.dumps(plan), finish="length"), attempts=1, latency_ms=1)
        with Tracer(self.root / "trace") as tracer:
            orchestrator = Orchestrator(provider=DeepSeekProvider("test-key"),
                                        run_dir=self.root / "run", settings=Settings(), tracer=tracer,
                                        root_acceptance=lambda _workspace: (True, "trusted test verifier"))
            result = orchestrator.run("work")
        self.assertEqual(result.status, "error")
        self.assertEqual(result.tokens, 15)
        self.assertEqual(result.subtasks, [])
        self.assertEqual(result.acceptance_report.verdict, "passed")
        self.assertEqual(summarize(load_events(self.root / "trace"))["goal_completed"], 0)
        self.assertEqual(post.call_count, 1)
        usage = next(e for e in load_events(self.root / "trace") if e["kind"] == "plan_usage")
        self.assertEqual(usage["finish_reason"], "length")
        self.assertEqual(usage["prompt_tokens"], 12)

    def test_offline_trace_has_no_http_attempts(self):
        with Tracer(self.root / "trace") as tracer:
            result = self.agent(ScriptedProvider([{"text": "done"}]), tracer).run("offline")
        self.assertEqual(result.usage.source, "estimated")
        summary = summarize(load_events(self.root / "trace"))
        self.assertEqual(summary["http_attempts"], 0)
        self.assertEqual(summary["http_attempts_unknown_calls"], 0)
        self.assertEqual(summary["usage_estimated_calls"], 1)

    def test_cli_wires_all_provider_options(self):
        settings = Settings(api_key="synthetic-key", thinking="enabled", reasoning_effort="max",
                            max_output_tokens=1234, request_timeout_seconds=7,
                            request_deadline_seconds=25, max_retries=1)
        provider = _make_provider_factory(None, settings)()
        self.assertEqual((provider.thinking, provider.reasoning_effort, provider.max_output_tokens),
                         ("enabled", "max", 1234))
        self.assertEqual((provider.timeout, provider.deadline_seconds, provider.max_retries), (7, 25, 1))

    @patch.dict(os.environ, {}, clear=True)
    @patch("miniagent.providers.post_json")
    def test_cli_missing_key_smoke_is_not_run(self, post):
        output = self.root / "probe.json"
        with contextlib.redirect_stdout(io.StringIO()):
            code = main(["provider-smoke", "--thinking", "enabled", "--output", str(output)])
        self.assertEqual(code, 2)
        report = json.loads(output.read_text())
        self.assertEqual(report["status"], "not_run")
        self.assertEqual(report["reason_code"], "missing_api_key")
        self.assertFalse(report["live_verified"])
        post.assert_not_called()

    @patch.dict(os.environ, {"DEEPSEEK_TIMEOUT": "private-invalid-input"}, clear=True)
    def test_cli_invalid_config_has_no_traceback_or_input_echo(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            code = main(["demo"])
        self.assertEqual(code, 2)
        self.assertNotIn("private-invalid-input", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())


if __name__ == "__main__":
    unittest.main()
