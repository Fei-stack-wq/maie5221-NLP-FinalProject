"""Pausing in the middle of parallel hand-offs or a review loop, restarting Studio, and resuming.

A finished parallel hand-off must not run again, the paused one continues where it
stopped, a review loop continues the same round, and member sessions survive the
checkpoint's JSON round trip.
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from miniagent.checkpoint import path_for
from miniagent.studio import Studio
from tests.helpers import FakeProvider, Script, call, reply, settings, tool_results
from tests.test_team import TEAM, wait_for


class PauseCollaborationTest(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.settings = settings(Path(root.name))
        self.script = Script()
        self.provider = FakeProvider(self.script)
        self.open()
        self.gid = self.studio.create_group("Team", TEAM, "lead", "autonomous")["id"]
        self.tid = None

    def open(self):
        self.studio = Studio(self.settings, lambda _: self.provider)
        self.addCleanup(self.studio.close)
        self.studio.restore_queue()

    def request_pause(self):
        self.assertEqual(self.studio.pause_task(self.tid)["status"], "pausing")

    def paused_then_restarted(self):
        wait_for(lambda: self.studio.group(self.gid).load_task(self.tid)["status"] == "paused")
        self.assertTrue(path_for(self.studio.group(self.gid), self.tid).is_file())
        self.studio.close()
        self.open()
        self.studio.resume_task(self.tid)
        wait_for(lambda: self.studio.group(self.gid).load_task(self.tid)["status"] == "completed", timeout=15)
        store = self.studio.group(self.gid)
        kinds = [e["kind"] for e in store.chat() if e.get("task_id") == self.tid]
        self.assertEqual((kinds.count("task_paused"), kinds.count("task_resumed")), (1, 1))
        return store

    def test_pause_during_parallel_hand_offs_keeps_the_finished_one(self):
        dev_done = threading.Event()

        def dev_last(messages):
            dev_done.set()
            return reply("dev wrote api/server.py")

        def qa_first(messages):
            dev_done.wait(5)
            self.request_pause()  # qa pauses at its next boundary, before running this tool call
            return reply("", call("write_file", path="docs/test-plan.md", content="plan\n"))

        seen = {}
        self.script.queues.update(
            lead=[reply("", call("delegate_task", assignee="dev", task="Write the backend in api/server.py.",
                                 deliverables=["api/"], independent=True),
                        call("delegate_task", assignee="qa", task="Write the test plan in docs/test-plan.md.",
                             deliverables=["docs/test-plan.md"], independent=True)),
                  lambda m: (seen.__setitem__("results", tool_results(m)), reply("both done"))[1]],
            dev=[reply("", call("write_file", path="api/server.py", content="server\n")), dev_last],
            qa=[qa_first, reply("qa wrote the test plan")])
        self.tid = self.studio.post(self.gid, "Start", queue=True)["task_id"]
        store = self.paused_then_restarted()

        record = store.load_task(self.tid)
        self.assertEqual((record["final_answer"], record["delegations"]), ("both done", 2))
        self.assertEqual(len(self.provider.for_agent("dev")), 2, "the finished hand-off did not run again")
        self.assertEqual(len(self.provider.for_agent("qa")), 2, "qa continued from its pause frame")
        self.assertEqual([r.splitlines()[0] for r in seen["results"]],
                         ["Report from Developer (dev) — status: completed", "Report from Tester (qa) — status: completed"])
        self.assertEqual((store.project.root / "docs/test-plan.md").read_text(), "plan\n")
        self.assertEqual((store.project.root / "api/server.py").read_text(), "server\n")
        kinds = [e["kind"] for e in store.chat() if e.get("task_id") == self.tid]
        self.assertEqual((kinds.count("delegation"), kinds.count("report")), (2, 2))
        trace = [json.loads(line) for line in (store.task_dir(self.tid) / "trace.jsonl").read_text().splitlines()]
        ids = [e["call_id"] for e in trace if e["type"] == "tool_call"]
        self.assertEqual(len(ids), len(set(ids)), "no tool call was announced twice")

    def test_pause_during_the_parallel_follow_up_check_finishes_it_on_resume(self):
        def dev_check(messages):
            self.assertIn("qa wrote the test plan", messages[-1]["content"])
            self.request_pause()  # dev pauses before running this tool call
            return reply("", call("write_file", path="api/server.py", content="server v2\n"))

        seen = {}
        self.script.queues.update(
            lead=[reply("", call("delegate_task", assignee="dev", task="Write the backend in api/server.py.",
                                 deliverables=["api/"], independent=True),
                        call("delegate_task", assignee="qa", task="Write the test plan in docs/test-plan.md.",
                             deliverables=["docs/test-plan.md"], independent=True)),
                  lambda m: (seen.__setitem__("results", tool_results(m)), reply("both done"))[1]],
            dev=[reply("", call("write_file", path="api/server.py", content="server\n")),
                 reply("dev wrote api/server.py"), dev_check, reply("Updated the server after reading the plan.")],
            qa=[reply("", call("write_file", path="docs/test-plan.md", content="plan\n")),
                reply("qa wrote the test plan"), reply("No change needed: the plan stands.")])
        self.tid = self.studio.post(self.gid, "Start", queue=True)["task_id"]
        store = self.paused_then_restarted()

        record = store.load_task(self.tid)
        self.assertEqual((record["final_answer"], record["delegations"]), ("both done", 2))
        self.assertEqual(len(self.provider.for_agent("dev")), 4, "dev's check continued from its pause frame")
        by_member = {r.splitlines()[0].split(" — ")[0]: r for r in seen["results"]}
        dev_result, qa_result = by_member["Report from Developer (dev)"], by_member["Report from Tester (qa)"]
        self.assertIn("dev wrote api/server.py", dev_result, "the first report survived the restart")
        self.assertIn("After seeing the parallel teammates' results: Updated the server after reading the plan.",
                      dev_result)
        self.assertIn("api/server.py (added)", dev_result)
        self.assertIn("After seeing the parallel teammates' results: No change needed", qa_result)
        self.assertEqual((store.project.root / "api/server.py").read_text(), "server v2\n")
        kinds = [e["kind"] for e in store.chat() if e.get("task_id") == self.tid]
        self.assertEqual(kinds.count("report"), 2)

    def test_pause_inside_a_review_loop_resumes_the_same_round(self):
        def dev_fix(messages):
            self.request_pause()
            return reply("", call("write_file", path="app.py", content="def add(a, b):\n    return a + b\n"))

        def qa_round_two(messages):
            self.assertTrue(any(m.get("content", "").startswith("VERDICT: CHANGES REQUESTED")
                                for m in messages if m["role"] == "assistant"),
                            "the reviewer's session survived the restart")
            return reply("VERDICT: APPROVED\nFloats work.")

        seen = {}
        self.script.queues.update(
            lead=[reply("", call("review_loop", author="dev", reviewer="qa", work="Test add() in app.py.",
                                 deliverables=["app.py"])),
                  lambda m: (seen.__setitem__("result", tool_results(m)[-1]), reply("shipped"))[1]],
            qa=[reply("VERDICT: CHANGES REQUESTED\n1. add(0.5, 0.5) returns 0."), qa_round_two],
            dev=[dev_fix, reply("Fixed add().")])
        (self.studio.group(self.gid).project.root / "app.py").write_text("def add(a, b):\n    return int(a) + int(b)\n")
        self.tid = self.studio.post(self.gid, "Review the adder", queue=True)["task_id"]
        store = self.paused_then_restarted()

        record = store.load_task(self.tid)
        self.assertEqual((record["final_answer"], record["delegations"]), ("shipped", 3))
        self.assertIn("APPROVED after 2 review round(s)", seen["result"])
        self.assertIn("Round 1 — review by Tester: CHANGES REQUESTED; fix by Developer: completed, 1 file(s) changed",
                      seen["result"])
        self.assertEqual(len(self.provider.for_agent("qa")), 2)
        self.assertEqual(len(self.provider.for_agent("dev")), 2)
        self.assertEqual(record["metrics"]["resumed_assignments"], 1)
        self.assertEqual((store.project.root / "app.py").read_text(), "def add(a, b):\n    return a + b\n")


if __name__ == "__main__":
    unittest.main()
