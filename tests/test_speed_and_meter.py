"""Task meter, budget notices, read-only batching, the warm browser and short browser timeouts.

The numbers behind these changes come from the Mid_Test Task 1 runs: 65–88 % of each run
was spent waiting on the model at about one tool call per response, and 50 failing browser
checks waited Playwright's 30 s default before failing.
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from miniagent.agent import Agent
from miniagent.browser_tool import BrowserSession, run_browser
from miniagent.config import Limits, TeamLimits
from miniagent.meter import TaskMeter, call_cost, price_factor
from miniagent.sandbox import Sandbox
from miniagent.schemas import ToolResult
from miniagent.studio import Studio, StudioError
from miniagent.team import member_prompt
from miniagent.tools import Tool, ToolContext, ToolRegistry, build_default_registry
from tests.helpers import FakeProvider, Script, call, reply, settings, tool_results
from tests.test_team import TEAM, finished

SATURDAY_NOON_UTC = datetime(2026, 11, 21, 12, tzinfo=timezone.utc).timestamp()
MONDAY_2AM_UTC = datetime(2026, 11, 23, 2, tzinfo=timezone.utc).timestamp()


class Clock:
    def __init__(self, at: float) -> None:
        self.at = at

    def __call__(self) -> float:
        return self.at


class MeterTest(unittest.TestCase):
    def test_prices_follow_peak_hours_and_cache_hits(self):
        self.assertEqual(price_factor(SATURDAY_NOON_UTC), 0.5)
        self.assertEqual(price_factor(MONDAY_2AM_UTC), 1.0)
        # 1M cached + 1M uncached + 1M output on deepseek-flash at peak: 0.006 + 0.30 + 1.20 USD.
        self.assertAlmostEqual(call_cost("deepseek-flash", 2_000_000, 1_000_000, 1_000_000, MONDAY_2AM_UTC), 1.506)

    def test_snapshot_totals_alerts_and_budget_notices(self):
        clock, notes = Clock(MONDAY_2AM_UTC), []
        meter = TaskMeter(budget_tokens=10_000, clock=clock, notify=notes.append)
        meter.record({"type": "start", "agent": "dev"})
        meter.record({"type": "step", "agent": "dev", "step": 1, "max_steps": 10})
        meter.record({"type": "model_call", "agent": "dev", "seconds": 4.0, "model": "deepseek-flash",
                      "prompt_tokens": 5_000, "cached_tokens": 4_000, "output_tokens": 500})
        self.assertEqual(len(notes), 1)
        self.assertIn("50% of its soft tokens budget", notes[0])
        meter.record({"type": "tool_call", "agent": "dev", "tool": "run_shell", "call_id": "c1"})
        clock.at += 70
        snap = meter.snapshot()
        self.assertEqual(snap["alerts"], [{"kind": "slow_tool", "agent": "dev", "tool": "run_shell", "seconds": 70}])
        meter.record({"type": "tool_result", "agent": "dev", "tool": "run_shell", "call_id": "c1", "seconds": 70.0})
        for step in range(2, 9):
            meter.record({"type": "step", "agent": "dev", "step": step, "max_steps": 10})
        meter.record({"type": "model_call", "agent": "dev", "seconds": 6.0, "model": "deepseek-flash",
                      "prompt_tokens": 6_000, "cached_tokens": 6_000, "output_tokens": 0})
        self.assertIn("80% of its soft tokens budget", notes[1])
        self.assertIn("The soft tokens budget is used up", notes[2])
        meter.record({"type": "model_call", "agent": "dev", "seconds": 1.0, "model": "deepseek-flash",
                      "prompt_tokens": 9_000, "cached_tokens": 0, "output_tokens": 0})
        self.assertEqual(len(notes), 3, "each mark is announced once")
        snap = meter.snapshot()
        self.assertEqual((snap["model_s"], snap["tool_s"], snap["model_calls"], snap["steps"]), (11.0, 70.0, 3, 8))
        self.assertEqual(snap["tokens"], {"prompt": 20_000, "cached": 10_000, "output": 500})
        self.assertEqual(snap["alerts"], [{"kind": "step_budget", "agent": "dev", "steps": 8, "max_steps": 10}])
        self.assertEqual(snap["slowest_tools"], [{"agent": "dev", "tool": "run_shell", "seconds": 70.0}])
        self.assertGreater(snap["cost_usd"], 0)
        clock.at += 200
        self.assertIn({"kind": "quiet", "seconds": 200}, meter.snapshot()["alerts"])

    def test_meter_is_safe_under_parallel_hand_offs(self):
        meter = TaskMeter()
        def feed(agent):
            for _ in range(500):
                meter.record({"type": "model_call", "agent": agent, "seconds": 0.01, "model": "deepseek-flash",
                              "prompt_tokens": 10, "cached_tokens": 5, "output_tokens": 1})
        threads = [threading.Thread(target=feed, args=(name,)) for name in ("dev", "qa", "docs")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(meter.snapshot()["model_calls"], 1500)


class TaskMonitoringTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_task_records_time_and_the_lead_gets_budget_notices(self):
        seen = {}
        script = Script(
            lead=[reply("", call("delegate_task", assignee="dev", task="Write notes.md about the API please.")),
                  lambda m: (seen.__setitem__("lead", m[-1]["content"]), reply("done"))[1]],
            dev=[reply("", call("write_file", path="notes.md", content="notes\n")), reply("Wrote notes.md.")])
        provider = FakeProvider(script)
        studio = Studio(settings(self.tmp), provider_factory=lambda _s: provider)
        gid = studio.create_group("Team", TEAM, "lead", "autonomous")["id"]
        studio.post(gid, "Write notes", queue=True, options={"budget_tokens": 50})
        task = finished(studio, gid)
        self.assertIn("[Studio budget notice]", seen["lead"], "the lead hears about the soft budget")
        self.assertEqual(task["status"], "completed", "a soft budget never stops the task")
        timing = task["metrics"]["time"]
        self.assertEqual(timing["model_calls"], 4)
        self.assertEqual({a["agent"] for a in timing["agents"]}, {"lead", "dev"})
        self.assertGreaterEqual(timing["elapsed_s"], timing["model_s"])
        trace = (studio.group(gid).task_dir(task["id"]) / "trace.jsonl").read_text()
        self.assertIn('"type": "model_call"', trace)
        self.assertIn('"seconds":', trace)

    def test_budget_options_are_validated(self):
        studio = Studio(settings(self.tmp), provider_factory=lambda _s: FakeProvider(Script()))
        gid = studio.create_group("Team", TEAM, "lead", "autonomous")["id"]
        for options in ({"budget_minutes": -1}, {"budget_tokens": "lots"}, {"budget_minutes": 99999}):
            with self.assertRaises(StudioError):
                studio.post(gid, "go", queue=True, options=options)

    def test_live_state_includes_the_meter(self):
        release = threading.Event()
        def lead_waits(messages):
            release.wait(5)
            return reply("done")
        provider = FakeProvider(Script(lead=[lead_waits]))
        studio = Studio(settings(self.tmp), provider_factory=lambda _s: provider)
        gid = studio.create_group("Team", TEAM, "lead", "autonomous")["id"]
        studio.post(gid, "go", queue=True)
        deadline = time.monotonic() + 5
        live = None
        while time.monotonic() < deadline and not (live and live.get("meter") and live["meter"]["agents"]):
            live = studio.state(gid)["live"]
            time.sleep(0.02)
        release.set()
        finished(studio, gid)
        self.assertIn("elapsed_s", live["meter"])
        self.assertEqual(live["meter"]["agents"][0]["agent"], "lead")


class BatchingTest(unittest.TestCase):
    def test_read_only_tools_in_one_response_run_together(self):
        barrier = threading.Barrier(2, timeout=5)
        def waiting(_ctx, args):
            barrier.wait()  # both calls must be running at the same time
            return ToolResult(ok=True, content="read " + args["path"])
        registry = ToolRegistry([
            Tool("peek_a", "", {"type": "object", "properties": {"path": {"type": "string"}}}, waiting, read_only=True),
            Tool("peek_b", "", {"type": "object", "properties": {"path": {"type": "string"}}}, waiting, read_only=True)])
        events = []
        provider = FakeProvider(Script(worker=[reply("", call("peek_a", path="a"), call("peek_b", path="b")), reply("done")]))
        root = Path(tempfile.mkdtemp())
        agent = Agent("worker", provider, registry, Sandbox(root, "worker"), settings(root),
                      system_prompt="You are Worker (id `worker`).", on_event=events.append)
        result = agent.run("read both")
        self.assertEqual(result.status, "completed")
        self.assertIn({"type": "parallel", "agent": "worker", "tool": "reads", "count": 2},
                      [{k: e[k] for k in ("type", "agent", "tool", "count")} for e in events if e["type"] == "parallel"])
        self.assertEqual(tool_results(provider.requests[-1]["messages"]), ["read a", "read b"])

    def test_writes_never_join_a_read_batch(self):
        registry = build_default_registry()
        self.assertTrue(registry.get("read_file").read_only and registry.get("http_get").read_only)
        self.assertFalse(registry.get("write_file").read_only or registry.get("run_shell").read_only)

    def test_prompts_ask_for_batching_and_owned_verification(self):
        lead, dev = (type("M", (), {"id": i, "name": n, "prompt": ""})() for i, n in (("lead", "Lead"), ("dev", "Dev")))
        text = member_prompt(dev, lead, [lead, dev], settings(Path(tempfile.mkdtemp())), False)
        self.assertIn("put independent tool calls in one response", text)
        self.assertIn("leave full end-to-end walk-throughs to the teammate who reviews", text)


@unittest.skipUnless(os.name == "posix", "run_browser needs macOS or Linux")
class WarmBrowserTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "index.html").write_text("<h1>Rooms</h1><script>console.error('boom')</script>")
        self.sandbox = Sandbox(self.root, "qa")
        self.sandbox.command_tmpdir = Path(tempfile.mkdtemp())
        self.ctx = ToolContext(sandbox=self.sandbox, limits=Limits())
        self.sandbox.browser_session = BrowserSession()
        self.addCleanup(self.sandbox.browser_session.close)

    def run_code(self, code, **extra):
        return run_browser(self.ctx, {"code": code, **extra})

    def test_one_browser_serves_many_checks_with_fresh_storage(self):
        open_page = "page.goto((WORKSPACE / 'index.html').as_uri()); "
        first = self.run_code(open_page + "print(page.inner_text('h1')); page.evaluate(\"localStorage.setItem('k','v')\")")
        pid = self.sandbox.browser_session.process.pid
        second = self.run_code(open_page + "print('stored', page.evaluate(\"localStorage.getItem('k')\"))")
        self.assertTrue(first.ok and second.ok, (first.content, second.content))
        self.assertIn("Rooms", first.content)
        self.assertIn("console.error: boom", first.content, "console errors are still reported")
        self.assertIn("stored None", second.content, "each call gets a fresh context")
        self.assertEqual(self.sandbox.browser_session.process.pid, pid, "the same browser server was reused")
        self.assertTrue(first.meta["warm_browser"])

    def test_child_output_exit_codes_and_closing_the_browser(self):
        child = self.run_code("import subprocess; subprocess.run(['echo', 'child-output']); raise SystemExit(3)")
        self.assertEqual((child.ok, child.meta["exit_code"]), (False, 3))
        self.assertIn("child-output", child.content)
        self.assertTrue(self.run_code("browser.close(); print('closed')").ok)
        self.assertTrue(self.run_code("page.goto((WORKSPACE / 'index.html').as_uri())").ok, "a closed browser is relaunched")

    def test_missing_elements_fail_in_seconds_not_half_a_minute(self):
        started = time.monotonic()
        result = self.run_code("page.goto((WORKSPACE / 'index.html').as_uri()); page.click('#book')")
        self.assertFalse(result.ok)
        self.assertIn("Timeout 5000ms", result.content)
        self.assertLess(time.monotonic() - started, 15)

    def test_a_hanging_script_is_stopped_and_the_next_call_starts_a_new_browser(self):
        result = self.run_code("import time; time.sleep(30)", timeout=2)
        self.assertFalse(result.ok)
        self.assertTrue(result.meta["timed_out"])
        self.assertIsNone(self.sandbox.browser_session.process)
        self.assertTrue(self.run_code("page.goto((WORKSPACE / 'index.html').as_uri())").ok)

    def test_agent_run_closes_its_browser(self):
        provider = FakeProvider(Script(qa=[reply("", call("run_browser", code="page.goto((WORKSPACE / 'index.html').as_uri())")),
                                           reply("checked")]))
        agent = Agent("qa", provider, build_default_registry(), Sandbox(self.root, "qa"), settings(self.root),
                      system_prompt="You are QA (id `qa`).")
        sessions = []
        original = BrowserSession._start
        def spy(session, ctx):
            sessions.append(session)
            return original(session, ctx)
        BrowserSession._start = spy
        try:
            result = agent.run("check the page")
        finally:
            BrowserSession._start = original
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(sessions), 1)
        self.assertIsNone(sessions[0].process, "the browser was closed when the run ended")


if __name__ == "__main__":
    unittest.main()
