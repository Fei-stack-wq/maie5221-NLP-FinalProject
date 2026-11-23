"""The lead's own files and steps around a hand-off, and hand-offs that run together by default.

From the v3.4 runs of 23 Nov: in S2T1 the chief editor wrote the brief, delegated the reporting with
it as an input, and the reporter was told the brief was "Not in the project yet" (it was published
right after the check); the plan step for the brief declared another path and stayed open. In S1T1
the lead meant QA and packaging to run in parallel but did not mark them independent.
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

PLAN = [{"id": "pick", "step": "Choose the story", "status": "completed", "owner": "lead", "on_delivery": False},
        {"id": "brief", "step": "Write the editorial brief", "status": "in_progress", "owner": "lead",
         "outputs": ["plan/editorial_brief.md"], "after": ["pick"]},
        {"id": "report", "step": "Report the story", "status": "pending", "owner": "dev", "after": ["brief"]}]


def assignment(messages):
    return next(m["content"] for m in messages if m["role"] == "user" and "Assignment from" in m["content"])


class LeadStepsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def studio(self, script):
        provider = FakeProvider(script)
        studio = Studio(settings(self.tmp), provider_factory=lambda _s: provider)
        self.addCleanup(studio.close)
        return studio, studio.create_group("Desk", TEAM4, "lead", "autonomous")["id"], provider

    def test_a_file_the_lead_just_wrote_counts_as_present(self):
        seen = {}
        script = Script(
            lead=[reply("", call("write_file", path="news/brief.md", content="Angle: AI common core.\n")),
                  reply("", call("delegate_task", assignee="dev", task="Report the story from the brief.",
                                 inputs=["news/brief.md"], deliverables=["news/draft.md"])),
                  lambda m: (seen.__setitem__("result", tool_results(m)[-1]), reply("done"))[1]],
            dev=[reply("drafted")])
        studio, gid, provider = self.studio(script)
        studio.post(gid, "Cover the story", queue=True)
        finished(studio, gid)
        brief = assignment(provider.for_agent("dev")[0]["messages"])
        self.assertNotIn("Not in the project yet", brief)
        self.assertIn("=== news/brief.md ===\nAngle: AI common core.", brief)
        self.assertNotIn("Inputs that were not in the project", seen["result"])
        delegation = next(e for e in studio.group(gid).chat() if e["kind"] == "delegation")
        publish = next(e for e in studio.group(gid).chat() if e["kind"] == "publish")
        self.assertLess(publish["seq"], delegation["seq"], "the lead's file was published before the hand-off")

    def test_an_unclosed_step_of_the_lead_is_pointed_out_with_what_it_published(self):
        seen = {}
        script = Script(
            lead=[reply("", call("update_plan", steps=PLAN)),
                  lambda m: (seen.__setitem__("plan", tool_results(m)[-1]),
                             reply("", call("write_file", path="news/brief.md", content="Angle.\n")))[1],
                  reply("", call("delegate_task", assignee="dev", task="Report the story from news/brief.md.",
                                 deliverables=["news/draft.md"], step="report")),
                  lambda m: (seen.__setitem__("result", tool_results(m)[-1]), reply("done"))[1]],
            dev=[reply("drafted")])
        studio, gid, _ = self.studio(script)
        studio.post(gid, "Cover the story", queue=True)
        finished(studio, gid)
        self.assertIn("brief Write the editorial brief (outputs: plan/editorial_brief.md)", seen["plan"])
        self.assertIn("Plan: this hand-off follows your own step brief (Write the editorial brief; expects "
                      "plan/editorial_brief.md, which is not in the project), which the plan does not show as done. "
                      "Files you published since the plan was written: news/brief.md. Close the step with "
                      "record_plan_result, or correct its outputs with update_plan.", seen["result"])

    def test_no_reminder_once_the_step_is_done(self):
        seen = {}
        script = Script(
            lead=[reply("", call("update_plan", steps=PLAN)),
                  reply("", call("write_file", path="plan/editorial_brief.md", content="Angle.\n")),
                  reply("", call("delegate_task", assignee="dev", task="Report the story from the brief.",
                                 deliverables=["news/draft.md"], step="report")),
                  lambda m: (seen.__setitem__("result", tool_results(m)[-1]), reply("done"))[1]],
            dev=[reply("drafted")])
        studio, gid, _ = self.studio(script)
        studio.post(gid, "Cover the story", queue=True)
        finished(studio, gid)
        self.assertNotIn("which the plan does not show as done", seen["result"])

    def test_hand_offs_in_one_response_run_together_without_being_marked(self):
        barrier = threading.Barrier(2, timeout=5)
        together = {}

        def first(name, path):
            def respond(messages):
                together[name] = barrier.wait() is not None
                return reply("", call("write_file", path=path, content=name + "\n"))
            return respond

        script = Script(
            lead=[reply("", call("delegate_task", assignee="qa", task="Test the finished build in app.py.",
                                 deliverables=["docs/QA.md"], kind="check"),
                        call("delegate_task", assignee="writer", task="Package the release.",
                             deliverables=["RELEASE_NOTES.md"], kind="make")),
                  reply("shipped")],
            qa=[first("qa", "docs/QA.md"), reply("VERDICT: APPROVED")],
            writer=[first("writer", "RELEASE_NOTES.md"), reply("packaged"), reply("No change needed.")])
        studio, gid, _ = self.studio(script)
        studio.post(gid, "Ship", queue=True)
        task = finished(studio, gid)
        self.assertEqual(together, {"qa": True, "writer": True})
        self.assertEqual(task["metrics"]["parallel_batches"], 1)


if __name__ == "__main__":
    unittest.main()


class BackgroundInBatchTest(unittest.TestCase):
    """S1T1-v3.5: QA (check) first and the release (adapt, background by default) second in one response."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def run_pair(self, release_inputs):
        started, seen = threading.Event(), {}

        def qa_first(messages):
            seen["release_started_first"] = started.wait(5)
            return reply("", call("write_file", path="QA_REPORT.md", content="pass\n"))

        def release_first(messages):
            started.set()
            return reply("", call("write_file", path="README.md", content="guide\n"))

        script = Script(
            lead=[reply("", call("delegate_task", assignee="qa", task="Test the finished build.",
                                 deliverables=["QA_REPORT.md"], kind="check"),
                        call("delegate_task", assignee="writer", task="Write the user guide for the build.",
                             deliverables=["README.md"], inputs=release_inputs, kind="adapt", background=True)),
                  lambda m: (seen.__setitem__("results", tool_results(m)[-2:]), reply("", call("wait_handoffs")))[1],
                  lambda m: (seen.__setitem__("waited", "\n".join([tool_results(m)[-1]] + [
                      x["content"] for x in m if x["role"] == "user"])), reply("shipped"))[1]],
            qa=[qa_first, reply("VERDICT: APPROVED")],
            writer=[release_first, reply("guide written")])
        provider = FakeProvider(script)
        studio = Studio(settings(self.tmp), provider_factory=lambda _s: provider)
        self.addCleanup(studio.close)
        gid = studio.create_group("Team", TEAM4, "lead", "autonomous")["id"]
        studio.post(gid, "Ship", queue=True)
        task = finished(studio, gid)
        return task, seen, studio, gid

    def test_a_background_hand_off_after_a_foreground_one_starts_at_the_same_time(self):
        task, seen, studio, gid = self.run_pair(["app.py"])
        self.assertTrue(seen["release_started_first"], "the release was running while QA worked")
        qa_result, release_result = seen["results"]
        self.assertTrue(qa_result.startswith("Report from Tester (qa)"))
        self.assertTrue(release_result.startswith("Started in the background: Writer (writer)"))
        self.assertNotIn("ran one after another", qa_result)
        self.assertIn("guide written", seen["waited"])
        self.assertEqual(task["final_answer"], "shipped")
        self.assertTrue(next(e for e in studio.group(gid).chat() if e["kind"] == "delegation"
                             and e["assignee"] == "writer").get("background"))

    def test_a_background_hand_off_that_reads_the_others_output_still_waits(self):
        task, seen, _, _ = self.run_pair(["QA_REPORT.md"])
        self.assertFalse(seen["release_started_first"], "the guide that reads QA's report started after QA")
        self.assertIn("Waited for qa to finish before starting: it reads QA_REPORT.md, which qa produces.",
                      seen["results"][1])


class RealRunShapesTest(unittest.TestCase):
    """The response shapes from the v3.5 and v3.6 runs of 23 Nov."""

    def test_a_plan_call_a_check_and_an_adapt_hand_off_in_one_response_start_together(self):
        barrier = threading.Barrier(2, timeout=5)
        together = {}

        def first(name):
            def respond(messages):
                together[name] = barrier.wait() is not None
                return reply(f"{name} done")
            return respond

        plan = [{"id": "build", "step": "Build", "status": "completed", "owner": "dev"},
                {"id": "qa", "step": "Test", "status": "pending", "owner": "qa", "after": ["build"]},
                {"id": "docs", "step": "Package", "status": "pending", "owner": "writer", "after": ["build"]}]
        script = Script(
            lead=[reply("", call("update_plan", steps=plan),
                        call("delegate_task", assignee="qa", task="Test the finished build.", kind="check",
                             deliverables=["QA_REPORT.md"], step="qa"),
                        call("delegate_task", assignee="writer", task="Package the release.", kind="adapt",
                             deliverables=["README.md"], step="docs")),
                  reply("shipped")],
            qa=[first("qa")], writer=[first("writer")])
        with tempfile.TemporaryDirectory() as tmp:
            provider = FakeProvider(script)
            studio = Studio(settings(Path(tmp)), provider_factory=lambda _s: provider)
            gid = studio.create_group("Team", TEAM4, "lead", "autonomous")["id"]
            studio.post(gid, "Ship", queue=True)
            task = finished(studio, gid)
            studio.close()
        self.assertEqual(together, {"qa": True, "writer": True})
        self.assertEqual(task["final_answer"], "shipped")
