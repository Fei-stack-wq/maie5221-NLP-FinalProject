"""Dependencies survive plan updates, and a refused parallel batch tells the lead why.

From the v3.3 runs of 23 Nov: S1T1's closing plan update dropped every `after`, and in S2T1 the
lead sent two reporter hand-offs marked `independent: true` that ran one after the other with no
explanation, because both went to the same teammate.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from miniagent.config import Settings
from miniagent.store import Member
from miniagent.studio import Studio
from miniagent.team import lead_prompt
from tests.helpers import FakeProvider, Script, call, reply, settings, tool_results
from tests.test_handoff_kinds import TEAM4
from tests.test_team import finished

PLAN = [{"id": "build", "step": "Build v1", "status": "in_progress", "owner": "dev"},
        {"id": "qa", "step": "Test v1", "status": "pending", "owner": "qa", "after": ["build"]},
        {"id": "release", "step": "Release notes", "status": "pending", "owner": "writer", "after": ["build"]},
        {"id": "show", "step": "Walkthrough", "status": "pending", "owner": "lead", "after": ["qa", "release"]}]


class PlanContinuityTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def studio(self, script):
        provider = FakeProvider(script)
        studio = Studio(settings(self.tmp), provider_factory=lambda _s: provider)
        self.addCleanup(studio.close)
        return studio, studio.create_group("Team", TEAM4, "lead", "autonomous")["id"]

    def plans(self, studio, gid):
        return [[s.get("after") for s in e["steps"]] for e in studio.group(gid).chat() if e["kind"] == "plan"]

    def test_a_status_update_without_after_keeps_the_dependencies(self):
        seen = {}
        closing = [{k: v for k, v in s.items() if k != "after"} | {"status": "completed"} for s in PLAN]
        reordered = [closing[0], closing[2], closing[1], closing[3]]
        cleared = [dict(closing[0]), {**closing[1], "after": []}, closing[2], closing[3]]
        script = Script(lead=[reply("", call("update_plan", steps=PLAN)),
                              reply("", call("update_plan", steps=closing)),
                              lambda m: (seen.__setitem__("kept", tool_results(m)[-1]),
                                         reply("", call("update_plan", steps=reordered)))[1],
                              reply("", call("update_plan", steps=cleared)),
                              reply("done")])
        studio, gid = self.studio(script)
        studio.post(gid, "Plan it", queue=True)
        self.assertEqual(finished(studio, gid)["status"], "completed")
        first, kept, moved, cleared_after = self.plans(studio, gid)
        self.assertEqual(first, [None, [1], [1], [2, 3]])
        self.assertEqual(kept, first, "omitting `after` keeps it")
        self.assertIn("Kept the dependencies of 3 step(s)", seen["kept"])
        self.assertEqual(moved, [None, [1], [1], [2, 3]], "kept references follow the step IDs when steps move")
        self.assertEqual(cleared_after, [None, None, [1], [2, 3]], "`after: []` clears a step's dependencies")

    def test_independent_hand_offs_for_one_teammate_say_why_they_ran_in_order(self):
        seen = {}
        script = Script(
            lead=[reply("", call("delegate_task", assignee="dev", task="Research AI course offerings.",
                                 deliverables=["research/courses.md"], independent=True),
                        call("delegate_task", assignee="dev", task="Research peer universities.",
                             deliverables=["research/peers.md"], independent=True)),
                  lambda m: (seen.__setitem__("results", tool_results(m)[-2:]), reply("done"))[1]],
            dev=[reply("courses researched"), reply("peers researched")])
        studio, gid = self.studio(script)
        studio.post(gid, "Research", queue=True)
        finished(studio, gid)
        for result in seen["results"]:
            self.assertIn("Marked independent, but the hand-offs in that response ran one after another: dev had more "
                          "than one of them, and one teammate works on one assignment at a time.", result)

    def test_a_dependency_inside_the_batch_is_named(self):
        seen = {}
        script = Script(
            lead=[reply("", call("delegate_task", assignee="dev", task="Write the data summary.",
                                 deliverables=["data/summary.md"], independent=True),
                        call("delegate_task", assignee="writer", task="Write the client introduction.",
                             deliverables=["intro.md"], inputs=["data/summary.md"], independent=True)),
                  lambda m: (seen.__setitem__("results", tool_results(m)[-2:]), reply("done"))[1]],
            dev=[reply("summary written")], writer=[reply("intro written")])
        studio, gid = self.studio(script)
        studio.post(gid, "Write", queue=True)
        finished(studio, gid)
        self.assertIn("writer reads data/summary.md, which dev produces in the same response", seen["results"][0])

    def test_no_note_when_nothing_was_marked_independent(self):
        seen = {}
        script = Script(
            lead=[reply("", call("delegate_task", assignee="dev", task="Research AI course offerings."),
                        call("delegate_task", assignee="dev", task="Research peer universities.")),
                  lambda m: (seen.__setitem__("results", tool_results(m)[-2:]), reply("done"))[1]],
            dev=[reply("courses researched"), reply("peers researched")])
        studio, gid = self.studio(script)
        studio.post(gid, "Research", queue=True)
        finished(studio, gid)
        self.assertFalse(any("Marked independent" in r for r in seen["results"]))

    def test_the_lead_is_told_how_dependencies_work(self):
        members = [Member(m["id"], m["name"], m["prompt"]) for m in TEAM4]
        prompt = lead_prompt(members[0], members, Settings(api_key=""), False, True)
        self.assertIn("steps whose `after` steps are done can be started together", prompt)
        self.assertIn("Dependencies are kept when a later update omits `after`", prompt)


if __name__ == "__main__":
    unittest.main()
