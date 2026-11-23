"""Dependencies survive plan updates, and a refused parallel batch tells the lead why.

From the v3.3 runs of 23 Nov: S1T1's closing plan update dropped every `after`. (Why hand-offs in one
response wait for each other is covered in test_collaboration_features.)
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

    def test_the_lead_is_told_how_dependencies_work(self):
        members = [Member(m["id"], m["name"], m["prompt"]) for m in TEAM4]
        prompt = lead_prompt(members[0], members, Settings(api_key=""), False, True)
        self.assertIn("steps whose `after` steps are done can be started together", prompt)
        self.assertIn("Dependencies are kept when a later update omits `after`", prompt)


if __name__ == "__main__":
    unittest.main()
