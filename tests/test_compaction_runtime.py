"""Normal Agent.run turns carry the model checkpoint and meter its auxiliary calls."""
import json
import tempfile
import unittest
from pathlib import Path

from miniagent.acceptance import accept_files
from miniagent.agent import Agent
from miniagent.config import Settings
from miniagent.metrics import Tracer, load_events, summarize
from miniagent.providers import Provider
from miniagent.rao import Budget
from miniagent.sandbox import Sandbox
from miniagent.schemas import LLMResponse, ToolCall, Usage
from miniagent.tools import build_default_registry


class MeasuredProvider(Provider):
    name = "integration-fixture"
    transport = "http"

    def __init__(self):
        self.main_usage = 0
        self.requests = []
        self.auxiliary_calls = 0

    def chat(self, messages, tools=None, temperature=0.0):
        self.requests.append(messages)
        index = len(self.requests)
        usage = Usage(prompt_tokens=len(json.dumps(messages).encode()) + 512, completion_tokens=40)
        self.main_usage += usage.total
        if index <= 9:
            call = ToolCall(id=f"read-{index}", name="read_file", arguments={"path": f"facts-{index}.txt"})
            return LLMResponse(tool_calls=[call], finish_reason="tool_calls", usage=usage)
        return LLMResponse(text="Inspection complete.", finish_reason="stop", usage=usage)

    def summarize_context(self, messages, *, max_output_tokens, deadline_seconds):
        checkpoint = {"goals":["inspect booking system"], "files":[], "decisions":["private-checkpoint-test-471"],
                      "checks":[], "blockers":[]}
        self.auxiliary_calls += 1
        return LLMResponse(text=json.dumps(checkpoint), finish_reason="stop", request_attempts=2,
                           usage=Usage(prompt_tokens=200, completion_tokens=50))


class CompactionRuntimeTest(unittest.TestCase):
    def test_subsequent_request_and_run_metrics_include_auxiliary_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sandbox = Sandbox(root/"workspace")
            for n in range(1, 10):
                (sandbox.root/f"facts-{n}.txt").write_text(f"fixture-{n} " + "e" * 3200)
            provider = MeasuredProvider()
            budget = Budget(tokens_left=500_000, steps_left=30)
            with Tracer(root/"trace") as tracer:
                agent = Agent("review", provider, build_default_registry(read_only=True), sandbox,
                              Settings(context_window_tokens=24_000, max_output_tokens=1200), tracer=tracer)
                agent.unit.enable_control_tools = False
                result = agent.run("inspect each fixture file", budget=budget,
                                   acceptance=accept_files("facts-1.txt", min_bytes=1))
            events = load_events(root/"trace")
            auxiliary = [e for e in events if e["kind"] == "compaction_usage"]
            self.assertEqual(result.status, "completed", result.error)
            self.assertGreater(provider.auxiliary_calls, 0)
            self.assertEqual(len(auxiliary), provider.auxiliary_calls)
            self.assertTrue(all(e["request_attempts"] == 2 for e in auxiliary))
            self.assertTrue(any("private-checkpoint-test-471" in str(messages) for messages in provider.requests))
            self.assertEqual(result.usage.total, provider.main_usage + 250 * provider.auxiliary_calls)
            self.assertEqual(budget.tokens_used, result.usage.total)
            self.assertEqual(summarize(events)["total_tokens"], result.usage.total)
            self.assertTrue(all("private-checkpoint-test-471" not in str(event) for event in auxiliary))


if __name__ == "__main__":
    unittest.main()
