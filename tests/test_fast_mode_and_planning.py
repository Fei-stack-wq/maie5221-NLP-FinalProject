"""Dependency-aware planning (hand-off inputs, plan steps with `after`) and fast mode.

The planning rules come from the fund run of 22 Nov, where a risk review started in
parallel with the research it depended on. Fast mode is the time-boxed profile.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from miniagent.bench import arm_settings
from miniagent.config import FAST_TASK_SECONDS, Settings, TeamLimits, fast_profile
from miniagent.studio import Studio, StudioError
from miniagent.team import render_plan
from tests.helpers import FakeProvider, Script, call, reply, settings, tool_results
from tests.test_team import TEAM, finished


def kinds(studio, gid, kind):
    return [e for e in studio.group(gid).chat() if e["kind"] == kind]


class PlanningTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def studio(self, respond, **changes):
        provider = respond if isinstance(respond, FakeProvider) else FakeProvider(respond)
        studio = Studio(settings(self.tmp, **changes), provider_factory=lambda _s: provider)
        gid = studio.create_group("Team", TEAM, "lead", "autonomous")["id"]
        return studio, gid, provider

    def test_an_assignment_that_reads_another_ones_output_waits_for_it(self):
        spans = {}

        def member(name, path):
            def first(messages):
                spans[name] = [time.monotonic()]
                time.sleep(0.05)
                return reply("", call("write_file", path=path, content=name))
            def last(messages):
                spans[name].append(time.monotonic())
                return reply(f"{name} done")
            return [first, last]

        script = Script(
            lead=[reply("", call("delegate_task", assignee="dev", task="Build the research summary from the archive.",
                                 deliverables=["research/summary.md"]),
                        call("delegate_task", assignee="qa", task="Assess the suitability of the portfolio.",
                             deliverables=["risk/assessment.md"], inputs=["research/"])),
                  reply("done")],
            dev=member("dev", "research/summary.md"), qa=member("qa", "risk/assessment.md"))
        studio, gid, _ = self.studio(script)
        studio.post(gid, "Prepare the proposal", queue=True)
        task = finished(studio, gid)
        first, second = sorted(spans.values())
        self.assertLessEqual(first[1], second[0])

    def test_missing_inputs_are_named_to_the_member_and_the_lead(self):
        seen = {}
        script = Script(
            lead=[reply("", call("delegate_task", assignee="qa", task="Review the release notes for accuracy.",
                                 inputs=["RELEASE_NOTES.md", "README.md"])),
                  lambda m: (seen.__setitem__("lead", tool_results(m)[-1]), reply("ok"))[1]],
            qa=[lambda m: (seen.__setitem__("qa", m[-1]["content"]), reply("Notes are missing."))[1]])
        studio, gid, _ = self.studio(script)
        (studio.group(gid).project.root / "README.md").write_text("readme\n")
        studio.post(gid, "Check the notes", queue=True)
        finished(studio, gid)
        self.assertIn("Read these project files first: RELEASE_NOTES.md, README.md", seen["qa"])
        self.assertIn("Not in the project yet: RELEASE_NOTES.md", seen["qa"])
        self.assertIn("Inputs that were not in the project when the assignment started: RELEASE_NOTES.md", seen["lead"])

    def test_plan_steps_name_earlier_steps_they_need(self):
        steps = [{"step": "Research the market", "status": "completed", "owner": "dev"},
                 {"step": "Draft the test plan", "status": "pending", "owner": "qa"},
                 {"step": "Write release notes", "status": "pending", "owner": "dev"},
                 {"step": "Assess risk", "status": "pending", "owner": "qa", "after": [1, 2]}]
        results = {}
        script = Script(
            lead=[reply("", call("update_plan", steps=[{"step": "A", "status": "pending", "after": [2]},
                                                      {"step": "B", "status": "pending"}])),
                  lambda m: (results.__setitem__("bad", tool_results(m)[-1]), reply("", call("update_plan", steps=steps)))[1],
                  lambda m: (results.__setitem__("good", tool_results(m)[-1]), reply("done"))[1]])
        studio, gid, _ = self.studio(script)
        studio.post(gid, "Plan it", queue=True)
        task = finished(studio, gid)
        self.assertIn("`after` may only name earlier steps", results["bad"])
        self.assertIn("Ready to start: steps 2, 3.", results["good"])
        self.assertEqual(task["plan"]["steps"][3]["after"], [1, 2])
        self.assertIn("[ ] 4. Assess risk — qa (after 1, 2)", render_plan(task["plan"]))
        self.assertEqual(render_plan({"steps": [{"step": "X", "status": "pending"}]}), "[ ] X",
                         "plans without dependencies look as before")

    def test_lead_prompt_names_safe_and_unsafe_parallel_work(self):
        provider = FakeProvider(Script(lead=[reply("done")]))
        studio = Studio(settings(self.tmp), provider_factory=lambda _s: provider)
        gid = studio.create_group("Team", TEAM, "lead", "autonomous")["id"]
        studio.post(gid, "go", queue=True)
        finished(studio, gid)
        prompt = provider.for_agent("lead")[0]["messages"][0]["content"]
        self.assertIn("release notes, a user guide or a test plan drafted while the build or the tests run", prompt)
        self.assertIn("a risk review before the research", prompt)


class FastModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_profile(self):
        s = fast_profile(Settings(model="deepseek-v4-pro", reviewer_model="deepseek-v4-pro"))
        self.assertEqual((s.model, s.thinking, s.reasoning_effort), ("deepseek-flash", "enabled", "low"))
        self.assertEqual((s.member().thinking, s.reviewer().thinking, s.reviewer().model), ("disabled", "enabled", "deepseek-flash"))
        self.assertEqual((s.limits.max_steps, s.team.lead_max_steps, s.team.max_delegations, s.team.task_seconds),
                         (30, 25, 4, FAST_TASK_SECONDS))
        self.assertTrue(s.team.plans)
        self.assertFalse(s.team.retrospective)
        self.assertEqual(fast_profile(Settings(thinking="disabled")).thinking, "disabled", "thinking off stays off")
        self.assertEqual(Settings().member().thinking, "enabled", "standard mode is unchanged")
        self.assertTrue(arm_settings(Settings(), "fast", 400).team.fast)

    def test_fast_task_thinks_only_where_it_decides(self):
        created = []
        lead_and_reviews = FakeProvider(Script(
            lead=[reply("", call("delegate_task", assignee="dev", task="Write app.py printing hello please.")),
                  reply("", call("delegate_task", assignee="qa", task="Test app.py and report problems.", review=True)),
                  reply("Done; simplified: no persistence.")],
            qa=[reply("VERDICT: APPROVED")]))
        workers = FakeProvider(Script(dev=[reply("", call("write_file", path="app.py", content="print('hello')\n")),
                                           reply("Wrote app.py.")]))

        def factory(s):
            created.append((s.thinking, s.reasoning_effort, s.team.fast))
            return workers if s.thinking == "disabled" else lead_and_reviews

        studio = Studio(settings(self.tmp), provider_factory=factory)
        gid = studio.create_group("Team", TEAM, "lead", "autonomous")["id"]
        studio.post(gid, "Build a hello app", queue=True, options={"mode": "fast"})
        task = finished(studio, gid)
        self.assertEqual(task["status"], "completed")
        self.assertIn(("enabled", "low", True), created)
        self.assertIn(("disabled", "low", True), created)
        self.assertEqual([r["agent"] for r in workers.requests], ["dev", "dev"], "workers run without thinking")
        self.assertEqual({r["agent"] for r in lead_and_reviews.requests}, {"lead", "qa"}, "the lead and the review think")
        self.assertTrue(kinds(studio, gid, "task_started")[0]["fast"])
        lead_request = lead_and_reviews.for_agent("lead")[0]
        self.assertIn("FAST MODE", lead_request["messages"][0]["content"])
        self.assertIn("update_plan", lead_request["tools"], "a fast task may write a linked plan once")
        self.assertIn("FAST MODE", workers.requests[0]["messages"][0]["content"])
        self.assertEqual(studio.state(gid)["group"]["id"], gid)

    def test_the_task_wraps_up_at_its_time_limit(self):
        def respond(agent, messages, tools, options):
            if options.get("tool_choice") == "none":
                return reply("Time is up: here is what exists and what is missing.")
            time.sleep(0.3)
            return reply("", call("list_dir", path=".", offset=len(messages)))
        provider = FakeProvider(respond)
        studio = Studio(settings(self.tmp, team=TeamLimits(lead_max_steps=200, task_seconds=2)),
                        provider_factory=lambda _s: provider)
        gid = studio.create_group("Team", TEAM, "lead", "autonomous")["id"]
        started = time.monotonic()
        studio.post(gid, "Keep looking", queue=True)
        task = finished(studio, gid, timeout=20)
        self.assertLess(time.monotonic() - started, 12)
        self.assertEqual(task["status"], "incomplete")
        self.assertEqual(task["final_answer"], "Time is up: here is what exists and what is missing.")

    def test_unknown_mode_is_refused(self):
        studio = Studio(settings(self.tmp), provider_factory=lambda _s: FakeProvider(Script()))
        gid = studio.create_group("Team", TEAM, "lead", "autonomous")["id"]
        with self.assertRaises(StudioError):
            studio.post(gid, "go", queue=True, options={"mode": "turbo"})


if __name__ == "__main__":
    unittest.main()
