"""The single-agent loop: termination, loop guard, protocol hygiene, steering."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from miniagent.agent import Agent
from miniagent.config import Limits
from miniagent.providers import DeepSeekProvider
from miniagent.sandbox import Sandbox
from miniagent.schemas import LLMResponse, ToolCall, Usage
from miniagent.tools import build_default_registry
from tests.helpers import FakeProvider, call, reply, settings, tool_results


def valid_history(messages):
    """Raises ValueError if a provider would reject this history."""
    DeepSeekProvider._validate_history(messages, require_reasoning=False)


class AgentLoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.sandbox = Sandbox(self.tmp / "ws", "a")

    def agent(self, provider, **limits):
        cfg = settings(self.tmp, limits=Limits(**{"max_steps": 12, **limits}))
        return Agent("a", provider, build_default_registry(), self.sandbox, cfg, system_prompt="You are A (id `a`).")

    def test_tool_then_answer_completes(self):
        responses = iter([reply("", call("write_file", path="x.txt", content="hi")), reply("done: wrote x.txt")])
        provider = FakeProvider(lambda *_: next(responses))
        result = self.agent(provider).run("write x.txt")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.answer, "done: wrote x.txt")
        self.assertEqual((self.sandbox.root / "x.txt").read_text(), "hi")
        self.assertEqual(result.steps, 2)
        valid_history(provider.requests[-1]["messages"])

    def test_step_budget_always_ends_with_a_report_without_tools(self):
        def respond(agent, messages, tools, options):
            if options.get("tool_choice") == "none":
                return reply("REPORT: listed the directory, nothing else done")
            return reply("", call("list_dir", path=".", offset=len(messages)))  # distinct calls: no loop
        provider = FakeProvider(respond)
        result = self.agent(provider, max_steps=5).run("never finishes")
        self.assertEqual(result.status, "max_steps")
        self.assertEqual(result.steps, 5)
        self.assertIn("REPORT", result.answer)
        final = provider.requests[-1]
        self.assertEqual(final["options"]["tool_choice"], "none")
        self.assertIn("Step budget of 5 reached", final["messages"][-1]["content"])
        valid_history(final["messages"])

    def test_budget_warning_before_the_end(self):
        provider = FakeProvider(lambda a, m, t, o: reply("r") if o.get("tool_choice") == "none"
                                else reply("", call("list_dir", path=".", offset=len(m))))
        self.agent(provider, max_steps=10).run("x")
        warned = [r for r in provider.requests if any("[Budget]" in (m.get("content") or "") for m in r["messages"])]
        self.assertTrue(warned)

    def test_reread_amnesia_loop_is_stopped_quickly(self):
        """Regression for the 4,436-step run: the lead re-read the same files in a cycle forever."""
        for i in range(5):
            (self.sandbox.root / f"f{i}.md").write_text(f"section {i}\n" * 50)
        def respond(agent, messages, tools, options):
            if options.get("tool_choice") == "none":
                return reply("FINAL: I kept re-reading; summary of what I know.")
            n = sum(1 for m in messages if m.get("role") == "tool")
            return reply("", call("read_file", path=f"f{n % 5}.md"))
        provider = FakeProvider(respond)
        result = self.agent(provider, max_steps=200).run("coordinate")
        self.assertEqual(result.status, "stuck")
        self.assertLess(result.steps, 30)
        self.assertIn("FINAL", result.answer)
        nudges = [m for m in provider.requests[-1]["messages"] if "[Loop guard]" in (m.get("content") or "")]
        self.assertEqual(len(nudges), 1, "one nudge before stopping")

    def test_identical_failing_call_is_nudged_then_stopped(self):
        provider = FakeProvider(lambda a, m, t, o: reply("gave up") if o.get("tool_choice") == "none"
                                else reply("", call("read_file", path="missing.txt")))
        result = self.agent(provider, max_steps=50).run("read it")
        self.assertEqual(result.status, "stuck")
        self.assertLessEqual(result.steps, 5)

    def test_recovering_after_a_nudge_is_not_punished(self):
        steps = []
        def respond(agent, messages, tools, options):
            steps.append(1)
            nudged = any("[Loop guard]" in (m.get("content") or "") for m in messages)
            if nudged:
                return reply("finished differently")
            return reply("", call("read_file", path="missing.txt"))
        result = self.agent(FakeProvider(respond)).run("x")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.answer, "finished differently")

    def test_every_tool_call_is_answered_even_over_the_per_step_limit_or_invalid(self):
        bad = ToolCall(id="bad1", name="read_file", arguments={}, raw_arguments="{oops", parse_error="invalid JSON")
        many = reply("", *[call("list_dir", path=".", offset=i) for i in range(4)])
        many = many.model_copy(update={"tool_calls": [bad, *many.tool_calls]})
        responses = iter([many, reply("ok")])
        provider = FakeProvider(lambda *_: next(responses))
        result = self.agent(provider, max_tool_calls_per_step=3).run("x")
        self.assertEqual(result.status, "completed")
        messages = provider.requests[-1]["messages"]
        valid_history(messages)
        results = tool_results(messages)
        self.assertEqual(len(results), 5)
        self.assertIn("not valid JSON", results[0])
        self.assertTrue(all("Not executed" in r for r in results[3:]))

    def test_output_cut_off_is_recovered(self):
        responses = iter([reply("partial", finish="length"), reply("complete answer")])
        provider = FakeProvider(lambda *_: next(responses))
        result = self.agent(provider).run("x")
        self.assertEqual(result.answer, "complete answer")
        self.assertIn("[Output limit]", provider.requests[-1]["messages"][-1]["content"])

    def test_steering_message_is_delivered_before_finishing(self):
        class Inbox:
            def __init__(self):
                self.items = []
            def take(self, agent):
                items, self.items = self.items, []
                return items
        inbox = Inbox()
        def respond(agent, messages, tools, options):
            if any("PLEASE ADD TESTS" in (m.get("content") or "") for m in messages):
                return reply("added tests too")
            inbox.items.append("PLEASE ADD TESTS")
            return reply("first answer")
        agent = self.agent(FakeProvider(respond))
        agent.inbox = inbox
        result = agent.run("x")
        self.assertEqual(result.answer, "added tests too")

    def test_cancel_stops_before_the_next_request(self):
        flag = {"stop": False}
        def respond(agent, messages, tools, options):
            flag["stop"] = True
            return reply("", call("list_dir", path="."))
        agent = self.agent(FakeProvider(respond))
        agent.should_cancel = lambda: flag["stop"]
        result = agent.run("x")
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(result.steps, 1)

    def test_provider_failure_is_an_error_result_not_an_exception(self):
        from miniagent.provider_http import ProviderError
        def respond(*_):
            raise ProviderError("down", code="network_error")
        result = self.agent(FakeProvider(respond)).run("x")
        self.assertEqual(result.status, "error")
        self.assertIn("network_error", result.error)

    def test_background_server_is_killed_when_the_run_ends(self):
        responses = iter([
            reply("", call("run_shell", command="sleep 300 > /dev/null 2>&1 & echo $! > pid.txt")),
            reply("started"),
        ])
        result = self.agent(FakeProvider(lambda *_: next(responses))).run("x")
        self.assertEqual(result.status, "completed")
        import os
        import time
        pid = int((self.sandbox.root / "pid.txt").read_text())
        for _ in range(30):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            self.fail("background process survived the run")

    def test_limits_reject_unbounded_steps(self):
        with self.assertRaises(ValueError):
            Limits(max_steps=0)


class CompactionInLoopTest(unittest.TestCase):
    def test_long_run_compacts_and_keeps_task_and_valid_history(self):
        tmp = Path(tempfile.mkdtemp())
        sandbox = Sandbox(tmp / "ws", "a")
        (sandbox.root / "big.txt").write_text("".join(f"line {i} " + "x" * 80 + "\n" for i in range(2000)))
        summaries = []
        def respond(agent, messages, tools, options):
            if tools is None:  # summarizer request
                summaries.append(messages)
                return LLMResponse(text="## Goal\nread big.txt\n## Facts Already Established\n- big.txt has 2000 lines",
                                   usage=Usage(prompt_tokens=50, completion_tokens=20), finish_reason="stop")
            n = sum(1 for m in messages if m.get("role") == "tool")
            if n >= 6:
                return reply("done reading")
            return reply("", call("read_file", path="big.txt", offset=n * 30000, limit=30000))
        provider = FakeProvider(respond)
        cfg = settings(tmp, compact_at_tokens=20_000, keep_recent_tokens=6_000)
        agent = Agent("a", provider, build_default_registry(), sandbox, cfg, system_prompt="You are A (id `a`).")
        result = agent.run("THE ORIGINAL TASK")
        self.assertEqual(result.status, "completed")
        self.assertGreaterEqual(result.compactions, 1)
        last = [r for r in provider.requests if r["tools"]][-1]["messages"]
        self.assertEqual(last[1]["content"], "THE ORIGINAL TASK")
        valid_history(last)


if __name__ == "__main__":
    unittest.main()
