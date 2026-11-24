"""Plan dependencies by step ID, the ready-steps note, and a check that runs alongside other work.

Taken from the S1T1-v3.2 run of 23 Nov: the lead wrote `after: ["build"]` and was refused twice;
after the build report, QA (step 3) and the release (step 6) were both unblocked but ran one after
the other, because nothing told the lead and a check could not join a parallel batch.
"""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from miniagent.studio import Studio
from tests.helpers import FakeProvider, Script, call, reply, settings, tool_results
from tests.test_handoff_kinds import TEAM4
from tests.test_team import finished

PLAN = [{"id": "build", "step": "Build v1", "status": "in_progress", "owner": "dev"},
        {"id": "qa", "step": "Test v1 end to end", "status": "pending", "owner": "qa", "after": ["build"]},
        {"id": "release", "step": "Write the release notes", "status": "pending", "owner": "writer", "after": ["build"]},
        {"id": "show", "step": "Final walkthrough", "status": "pending", "owner": "lead", "after": ["qa", "release"]}]


class PlanOverlapTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def studio(self, script):
        provider = FakeProvider(script)
        studio = Studio(settings(self.tmp), provider_factory=lambda _s: provider)
        self.addCleanup(studio.close)
        return studio, studio.create_group("Team", TEAM4, "lead", "autonomous")["id"], provider

    def chat(self, studio, gid, kind):
        return [e for e in studio.group(gid).chat() if e["kind"] == kind]

    def test_after_accepts_step_ids_numbers_and_explains_a_bad_reference(self):
        seen = {}
        bad = [dict(PLAN[0]), {**PLAN[1], "after": ["biuld"]}]
        mixed = [dict(PLAN[0]), {**PLAN[1], "after": [1]}, {**PLAN[2], "after": ["1"]}, dict(PLAN[3])]
        script = Script(lead=[reply("", call("update_plan", steps=bad)),
                              lambda m: (seen.__setitem__("bad", tool_results(m)[-1]),
                                         reply("", call("update_plan", steps=mixed)))[1],
                              lambda m: (seen.__setitem__("ok", tool_results(m)[-1]), reply("planned"))[1]])
        studio, gid, _ = self.studio(script)
        studio.post(gid, "Plan the release", queue=True)
        self.assertEqual(finished(studio, gid)["status"], "completed")
        self.assertIn("Step 2 (qa): `after` may only name earlier steps, and 'biuld' is not one. Use numbers 1–1 or IDs build.",
                      seen["bad"])
        self.assertTrue(seen["ok"].startswith("Plan updated"))
        plan = self.chat(studio, gid, "plan")[-1]
        self.assertEqual([s.get("after") for s in plan["steps"]], [None, [1], [1], [2, 3]])

    def test_a_report_names_the_steps_that_can_start_together_and_a_check_runs_alongside(self):
        barrier = threading.Barrier(2, timeout=5)
        seen = {}

        def first(name, path):
            def respond(messages):
                seen[name + "_together"] = barrier.wait() is not None
                return reply("", call("write_file", path=path, content=name + "\n"))
            return respond

        script = Script(
            lead=[reply("", call("update_plan", steps=PLAN)),
                  reply("", call("delegate_task", assignee="dev", task="Build v1 in app.py.", deliverables=["app.py"],
                                 kind="make", step="build")),
                  lambda m: (seen.__setitem__("build", tool_results(m)[-1]),
                             reply("", call("delegate_task", assignee="qa", task="Test the finished build in app.py.",
                                            deliverables=["docs/QA.md"], inputs=["app.py"], kind="check", step="qa"),
                                   call("delegate_task", assignee="writer", task="Write release notes for the build.",
                                        deliverables=["RELEASE_NOTES.md"], inputs=["app.py"], kind="make", step="release")))[1],
                  lambda m: (seen.__setitem__("pair", tool_results(m)[-2:]), reply("shipped"))[1]],
            dev=[reply("", call("write_file", path="app.py", content="print('v1')\n")), reply("built")],
            qa=[first("qa", "docs/QA.md"), reply("VERDICT: APPROVED\nAll flows pass.")],
            writer=[first("writer", "RELEASE_NOTES.md"), reply("notes drafted")])
        studio, gid, provider = self.studio(script)
        studio.post(gid, "Ship v1", queue=True)
        task = finished(studio, gid)
        self.assertEqual(task["final_answer"], "shipped")
        self.assertIn("Plan: steps 2 qa (qa); 3 release (writer) can start now", seen["build"])
        self.assertTrue(seen["qa_together"] and seen["writer_together"], "the check and the release ran at once")
        qa_result, writer_result = seen["pair"]
        self.assertIn("VERDICT: APPROVED", qa_result)
        self.assertIn("notes drafted", writer_result)
        self.assertEqual((len(provider.for_agent("qa")), len(provider.for_agent("writer"))), (2, 2), "no follow-up turn")

    def test_a_check_that_lists_a_batch_mates_file_in_inputs_waits_for_it(self):
        # Only declared inputs hold a hand-off back; a brief that merely names the file does not (24 Nov runs).
        seen = {}

        def qa_first(messages):
            seen["writer_done_first"] = any(e["kind"] == "report" and e["agent"] == "writer"
                                            for e in holder["studio"].group(holder["gid"]).chat())
            return reply("checked RELEASE_NOTES.md")

        holder = {}
        script = Script(
            lead=[reply("", call("delegate_task", assignee="writer", task="Write release notes for v1.",
                                 deliverables=["RELEASE_NOTES.md"], kind="make"),
                        call("delegate_task", assignee="qa", task="Check RELEASE_NOTES.md for accuracy.",
                             deliverables=["docs/QA.md"], inputs=["RELEASE_NOTES.md"], kind="check")),
                  reply("done")],
            writer=[reply("notes written")], qa=[qa_first])
        studio, gid, _ = self.studio(script)
        holder.update(studio=studio, gid=gid)
        studio.post(gid, "Release", queue=True)
        finished(studio, gid)
        self.assertTrue(seen["writer_done_first"], "the check of the release notes ran after them")

    def test_no_note_without_dependencies_in_the_plan(self):
        seen = {}
        steps = [{k: v for k, v in s.items() if k != "after"} for s in PLAN]
        script = Script(lead=[reply("", call("update_plan", steps=steps)),
                              reply("", call("delegate_task", assignee="dev", task="Build v1 in app.py.", step="build")),
                              lambda m: (seen.__setitem__("build", tool_results(m)[-1]), reply("done"))[1]],
                        dev=[reply("built")])
        studio, gid, _ = self.studio(script)
        studio.post(gid, "Build", queue=True)
        finished(studio, gid)
        self.assertNotIn("can start now", seen["build"])


if __name__ == "__main__":
    unittest.main()
