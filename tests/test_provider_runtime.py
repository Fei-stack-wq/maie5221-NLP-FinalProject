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
    def test_truncated_response_never_executes_or_passes_acceptance(self, post):
        call = {"id": "c1", "type": "function", "function": {"name": "write_file",
                "arguments": '{"path":"bad.md","content":"must not write"}'}}
        post.return_value = HTTPResult(envelope(calls=[call], finish="length"), attempts=1, latency_ms=1)
        with Tracer(self.root / "trace") as tracer:
            result = self.agent(DeepSeekProvider("test-key"), tracer).run("truncated")
        self.assertEqual(result.status, "error")
        self.assertEqual(result.tool_calls, 0)
        self.assertEqual(result.usage.total, 15)
        self.assertFalse((self.root / "agent" / "bad.md").exists())
        self.assertNotIn("acceptance", [e["kind"] for e in load_events(self.root / "trace")])

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
