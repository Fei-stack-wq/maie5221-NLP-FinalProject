"""Hand-off kinds, preloaded inputs, batching, background hand-offs and evidence.

Every test posts a message to a team through the normal Studio entry and drives the
lead and members with an offline model, then checks what each agent was given, which
settings its provider was built with, and what reached the chat and the task record.
"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from miniagent.checkpoint import path_for
from miniagent.config import Limits, Settings, TeamLimits, fast_profile, kind_settings
from miniagent.studio import Studio
from tests.helpers import FakeProvider, Script, call, reply, settings, tool_results
from tests.test_team import TEAM, finished, wait_for

TEAM4 = TEAM + [{"id": "writer", "name": "Writer", "prompt": "Writes release notes and announcements."}]


def assignment(messages: list[dict]) -> str:
    return next(m["content"] for m in messages if m["role"] == "user" and "Assignment from" in m["content"])


def notes(messages: list[dict]) -> list[str]:
    return [m["content"] for m in messages if m["role"] == "user"]


class KindSettingsTest(unittest.TestCase):
    def test_kinds_only_lower_cost_against_what_the_requester_chose(self):
        base = Settings(api_key="")
        self.assertEqual((base.thinking, base.reasoning_effort), ("enabled", "high"))
        make, check, adapt = (kind_settings(base, k) for k in ("make", "check", "adapt"))
        self.assertEqual((make.thinking, make.reasoning_effort), ("enabled", "low"))
        self.assertEqual((check.thinking, check.reasoning_effort), ("enabled", "high"), "check keeps the effort")
        self.assertEqual((adapt.thinking, adapt.limits.tool_timeout_seconds), ("disabled", 60))
        self.assertEqual(check.limits.browser_timeout_seconds, 60)
        self.assertEqual(make.limits, base.limits)
        low = Settings(api_key="", reasoning_effort="low", thinking="disabled")
        self.assertEqual(kind_settings(low, "check").reasoning_effort, "low", "a kind never raises the effort")
        self.assertEqual(kind_settings(low, "research").thinking, "disabled", "a kind never turns thinking on")
        fast = fast_profile(base)
        self.assertEqual(kind_settings(fast, "make").thinking, "disabled", "fast mode's workers stay without thinking")
        self.assertEqual(kind_settings(fast, "check").thinking, "enabled")


class HandoffKindsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.made: list[Settings] = []

    def studio(self, respond, members=TEAM4, **changes):
        provider = FakeProvider(respond)
        changes.setdefault("limits", Limits(max_steps=150))

        def factory(s):
            self.made.append(s)
            return provider
        studio = Studio(settings(self.tmp, **changes), provider_factory=factory)
        self.addCleanup(studio.close)
        group = studio.create_group("Team", members, "lead", "autonomous")
        return studio, group["id"], provider

    def chat(self, studio, gid, kind):
        return [e for e in studio.group(gid).chat() if e["kind"] == kind]

    def test_each_kind_gets_its_thinking_budget_and_tools(self):
        script = Script(
            lead=[reply("", call("delegate_task", assignee="dev", task="Build the export button in app.py.",
                                 deliverables=["app.py"], kind="make")),
                  reply("", call("delegate_task", assignee="writer", task="Write release notes for the export.",
                                 deliverables=["RELEASE_NOTES.md"], kind="adapt", background=False)),
                  reply("done")],
            dev=[reply("dev built it")], writer=[reply("notes written")])
        studio, gid, provider = self.studio(script)
        studio.post(gid, "Ship the export button", queue=True)
        task = finished(studio, gid)
        self.assertEqual(task["status"], "completed")
        built = {(s.thinking, s.reasoning_effort) for s in self.made}
        self.assertIn(("enabled", "low"), built, "make runs at low effort")
        self.assertIn(("disabled", "low"), built, "adapt runs without thinking")
        dev, writer = provider.for_agent("dev")[0], provider.for_agent("writer")[0]
        self.assertIn("run_browser", dev["tools"])
        self.assertNotIn("run_browser", writer["tools"], "adapt has no browser")
        self.assertIn("Hand-off kind: make", assignment(dev["messages"]))
        self.assertIn("Step budget for this hand-off: 100 steps.", assignment(dev["messages"]))
        self.assertIn("Step budget for this hand-off: 25 steps.", assignment(writer["messages"]))
        self.assertEqual([e.get("handoff_kind") for e in self.chat(studio, gid, "delegation")], ["make", "adapt"])
        self.assertEqual([e.get("handoff_kind") for e in self.chat(studio, gid, "report")], ["make", "adapt"])
        self.assertEqual(task["metrics"]["handoff_kinds"], {"make": 1, "adapt": 1})
        self.assertNotIn("Hand-off kind", dev["messages"][0]["content"], "the system prompt is the same for every kind")

    def test_untagged_hand_offs_keep_todays_settings(self):
        script = Script(lead=[reply("", call("delegate_task", assignee="dev", task="Build the export button.")),
                              reply("done")], dev=[reply("built")])
        studio, gid, provider = self.studio(script)
        studio.post(gid, "Ship it", queue=True)
        finished(studio, gid)
        self.assertNotIn(("enabled", "low"), {(s.thinking, s.reasoning_effort) for s in self.made})
        dev = provider.for_agent("dev")[0]
        self.assertIn("run_browser", dev["tools"])
        self.assertNotIn("Hand-off kind", assignment(dev["messages"]))
        self.assertNotIn("handoff_kind", self.chat(studio, gid, "delegation")[0])

    def test_review_true_is_a_check_and_review_loop_rounds_are_check_and_fix(self):
        script = Script(
            lead=[reply("", call("delegate_task", assignee="qa", task="Test the export button end to end.", review=True)),
                  reply("", call("review_loop", author="dev", reviewer="qa", work="The export button in app.py.",
                                 max_rounds=2)),
                  reply("done")],
            qa=[reply("Tested: works."), reply("VERDICT: CHANGES REQUESTED\n1. The CSV header is missing."),
                reply("VERDICT: APPROVED")],
            dev=[reply("Added the header.")])
        studio, gid, provider = self.studio(script)
        studio.post(gid, "Check the export", queue=True)
        finished(studio, gid)
        self.assertEqual([e.get("handoff_kind") for e in self.chat(studio, gid, "delegation")], ["check", "check", "fix", "check"])
        self.assertIn("Hand-off kind: check", assignment(provider.for_agent("qa")[0]["messages"]))
        fix = provider.for_agent("dev")[0]["messages"]
        self.assertIn("Hand-off kind: fix", assignment(fix))
        self.assertNotIn("## Earlier hand-offs", assignment(fix), "a review-loop fix already has the findings")

    def test_inputs_are_preloaded_and_adapt_gets_the_earlier_evidence(self):
        checks = [{"kind": "exists", "path": "CHANGES.md"}]
        script = Script(
            lead=[reply("", call("delegate_task", assignee="dev", task="Build the export and log it in CHANGES.md.",
                                 deliverables=["CHANGES.md"], kind="make", checks=checks)),
                  reply("", call("delegate_task", assignee="writer", task="Announce the export to customers.",
                                 deliverables=["ANNOUNCEMENT.md"], inputs=["CHANGES.md", "docs"], kind="adapt",
                                 background=False)),
                  reply("done")],
            dev=[reply("", call("write_file", path="CHANGES.md", content="- Added CSV export button\n"),
                       call("write_file", path="docs/guide.md", content="Click Export.\n")),
                 reply("dev built the export; unit tests pass")],
            writer=[reply("announced")])
        studio, gid, provider = self.studio(script)
        studio.post(gid, "Ship and announce", queue=True)
        finished(studio, gid)
        brief = assignment(provider.for_agent("writer")[0]["messages"])
        self.assertIn("Read these project files first: CHANGES.md, docs", brief)
        self.assertIn("=== CHANGES.md ===\n- Added CSV export button", brief)
        self.assertIn("=== docs/guide.md ===\nClick Export.", brief, "a folder input contributes its files")
        self.assertIn("## Finished work to adapt", brief)
        self.assertIn("dev built the export; unit tests pass", brief)
        self.assertIn("Studio checks: 1/1 passed", brief)

    def test_single_reads_are_nudged_and_read_files_reads_several(self):
        seen = {}
        script = Script(
            lead=[reply("", call("write_file", path="a.md", content="alpha\n"), call("write_file", path="b.md", content="beta\n")),
                  reply("", call("delegate_task", assignee="dev", task="Summarise a.md and b.md.", kind="research")),
                  reply("done")],
            dev=[reply("", call("read_file", path="a.md")), reply("", call("read_file", path="b.md")),
                 reply("", call("list_dir", path=".")),
                 lambda m: (seen.__setitem__("nudge", m[-1]["content"]),
                            reply("", call("read_files", paths=["a.md", "b.md", "missing.md"])))[1],
                 lambda m: (seen.__setitem__("both", tool_results(m)[-1]), reply("summary"))[1]])
        studio, gid, _ = self.studio(script)
        studio.post(gid, "Summarise", queue=True)
        finished(studio, gid)
        self.assertIn("[Studio] Several single reads in a row", seen["nudge"])
        self.assertIn("=== a.md ===\nalpha", seen["both"])
        self.assertIn("=== b.md ===\nbeta", seen["both"])
        self.assertIn("=== missing.md ===\nError", seen["both"])

    def test_background_hand_off_returns_at_once_and_wait_collects_the_report(self):
        release, seen = threading.Event(), {}

        def lead_second(m):
            seen["started"] = tool_results(m)[-1]
            release.set()
            return reply("", call("wait_handoffs"))

        def writer_first(m):
            seen["waited_for_lead"] = release.wait(5)
            return reply("", call("write_file", path="RELEASE_NOTES.md", content="v1 notes\n"))

        script = Script(
            lead=[reply("", call("delegate_task", assignee="writer", task="Write release notes for v1.",
                                 deliverables=["RELEASE_NOTES.md"], kind="adapt", background=True)),
                  lead_second,
                  lambda m: (seen.__setitem__("waited", tool_results(m)[-1]),
                             reply("", call("read_file", path="RELEASE_NOTES.md")))[1],
                  lambda m: (seen.__setitem__("read", tool_results(m)[-1]), reply("shipped"))[1]],
            writer=[writer_first, reply("Release notes written.")])
        studio, gid, _ = self.studio(script)
        studio.post(gid, "Release v1", queue=True)
        task = finished(studio, gid)
        self.assertTrue(seen["waited_for_lead"], "the lead got control back while the writer worked")
        self.assertTrue(seen["started"].startswith("Started in the background: Writer (writer)"))
        self.assertIn("Background hand-off finished. Report from Writer (writer) — status: completed", seen["waited"])
        self.assertIn("Release notes written.", seen["waited"])
        self.assertIn("v1 notes", seen["read"], "the lead's workspace has the published file")
        self.assertEqual(task["final_answer"], "shipped")
        self.assertTrue(self.chat(studio, gid, "delegation")[0]["background"])
        self.assertTrue(self.chat(studio, gid, "report")[0]["background"])
        self.assertEqual(task["metrics"]["background_handoffs"], 1)

    def test_background_report_arrives_as_a_message_at_the_next_step(self):
        seen = {}
        holder = {}

        def lead_second(m):
            store = holder["studio"].group(holder["gid"])
            wait_for(lambda: any(e["kind"] == "report" for e in store.chat()), timeout=5)
            time.sleep(0.3)  # the report is delivered right after it is recorded
            return reply("", call("list_dir", path="."))

        script = Script(
            lead=[reply("", call("delegate_task", assignee="writer", task="Draft the announcement post.",
                                 deliverables=["post.md"], kind="adapt", background=True)),
                  lead_second,
                  lambda m: (seen.__setitem__("notes", notes(m)), reply("done"))[1]],
            writer=[reply("", call("write_file", path="post.md", content="post\n")), reply("Post drafted.")])
        studio, gid, _ = self.studio(script)
        holder.update(studio=studio, gid=gid)
        studio.post(gid, "Announce", queue=True)
        task = finished(studio, gid)
        self.assertTrue(any(n.startswith("Background hand-off finished. Report from Writer") for n in seen["notes"]))
        self.assertEqual(task["final_answer"], "done")

    def test_lead_that_answers_early_waits_and_updates_its_answer(self):
        answered, seen = threading.Event(), {}

        def lead_second(m):
            answered.set()
            return reply("first answer")

        def writer_first(m):
            answered.wait(5)
            time.sleep(0.2)
            return reply("Release notes written.")

        script = Script(
            lead=[reply("", call("delegate_task", assignee="writer", task="Write release notes for v1.",
                                 deliverables=["RELEASE_NOTES.md"], kind="adapt", background=True)),
                  lead_second,
                  lambda m: (seen.update(follow_up=notes(m)[-1], waited=tool_results(m)[-1]),
                             reply("final with notes"))[1]],
            writer=[writer_first])
        studio, gid, _ = self.studio(script)
        studio.post(gid, "Release v1", queue=True)
        task = finished(studio, gid)
        self.assertIn("You answered while hand-offs you started in the background were still open", seen["follow_up"])
        self.assertIn("Background hand-off finished. Report from Writer", seen["waited"])
        self.assertIn("Release notes written.", seen["waited"])
        self.assertEqual(task["final_answer"], "final with notes")
        self.assertEqual([e["text"] for e in self.chat(studio, gid, "final")], ["final with notes"])

    def test_a_hand_off_waits_for_a_busy_teammate_and_for_files_still_being_written(self):
        go, seen = threading.Event(), {}

        def writer_first(m):
            go.wait(5)
            return reply("notes written")

        def lead_second(m):
            go.set()
            return reply("", call("delegate_task", assignee="writer", task="Also write the blog post.", kind="adapt"),
                         call("delegate_task", assignee="dev", task="Fix typos in the release notes.",
                              deliverables=["RELEASE_NOTES.md"], kind="fix"))

        script = Script(
            lead=[reply("", call("delegate_task", assignee="writer", task="Write release notes for v1.",
                                 deliverables=["RELEASE_NOTES.md"], kind="adapt", background=True)),
                  lead_second,
                  lambda m: (seen.__setitem__("results", tool_results(m)[-2:]), reply("done"))[1]],
            writer=[writer_first, reply("blog written")], dev=[reply("typos fixed")])
        studio, gid, _ = self.studio(script)
        studio.post(gid, "Release v1", queue=True)
        task = finished(studio, gid)
        blog, typos = seen["results"]
        self.assertIn("Waited for writer to finish before starting: writer works on one assignment at a time.", blog)
        self.assertIn("blog written", blog)
        self.assertIn("Waited for writer to finish before starting: both write RELEASE_NOTES.md.", typos)
        self.assertEqual(task["delegations"], 3)

    def test_switches_off_restore_the_synchronous_untagged_hand_off(self):
        team = TeamLimits(lead_max_steps=12, handoff_kinds=False, background_handoffs=False, preload_inputs=False,
                          batch_nudge=False, handoff_evidence=False)
        schemas, seen = {}, {}
        script = Script(lead=[reply("", call("delegate_task", assignee="writer", task="Write the release notes.")),
                              lambda m: (seen.__setitem__("result", tool_results(m)[-1]), reply("done"))[1]],
                        writer=[reply("notes")])

        def respond(agent, messages, tools, options):
            for tool in tools or []:
                schemas.setdefault(agent, {})[tool.name] = tool.parameters
            return script(agent, messages, tools, options)
        studio, gid, provider = self.studio(respond, team=team)
        studio.post(gid, "Release", queue=True)
        task = finished(studio, gid)
        self.assertEqual(task["final_answer"], "done")
        self.assertTrue(seen["result"].startswith("Report from Writer"), "the hand-off ran synchronously")
        self.assertNotIn("wait_handoffs", schemas["lead"])
        self.assertFalse({"kind", "background"} & schemas["lead"]["delegate_task"]["properties"].keys())
        self.assertIn("run_browser", provider.for_agent("writer")[0]["tools"])
        self.assertNotIn("handoff_kind", self.chat(studio, gid, "delegation")[0])

    def run_adapt(self, options):
        script = Script(lead=[reply("", call("delegate_task", assignee="writer", task="Write the release notes.",
                                             kind="adapt", background=False)), reply("done")],
                        writer=[reply("notes")])
        studio, gid, provider = self.studio(script)
        studio.post(gid, "Release", queue=True, options=options)
        self.assertEqual(finished(studio, gid)["status"], "completed")
        return provider.for_agent("writer")[0]

    def test_an_explicit_thinking_choice_holds_for_every_kind(self):
        writer = self.run_adapt({"thinking": "enabled"})
        self.assertEqual({(s.thinking, s.reasoning_effort) for s in self.made}, {("enabled", "high")},
                         "every role thinks, at the chosen effort, when the task asks for thinking")
        self.assertNotIn("run_browser", writer["tools"], "the kind still sets the tools")
        self.assertIn("Step budget for this hand-off: 25 steps.", assignment(writer["messages"]))
        self.made.clear()
        self.run_adapt({"thinking": "disabled"})
        self.assertEqual({s.thinking for s in self.made}, {"disabled"}, "no role thinks when the task turns it off")


class BackgroundPauseTest(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.settings = settings(Path(root.name))
        self.script = Script()
        self.provider = FakeProvider(self.script)
        self.open()
        self.gid = self.studio.create_group("Team", TEAM4, "lead", "autonomous")["id"]

    def open(self):
        self.studio = Studio(self.settings, lambda _: self.provider)
        self.addCleanup(self.studio.close)
        self.studio.restore_queue()

    def test_pause_while_a_background_hand_off_runs_and_resume_it(self):
        paused, seen = threading.Event(), {}

        def writer_first(m):
            self.assertEqual(self.studio.pause_task(self.tid)["status"], "pausing")
            paused.set()
            return reply("", call("write_file", path="RELEASE_NOTES.md", content="v1 notes\n"))

        def lead_second(m):
            paused.wait(5)
            return reply("", call("wait_handoffs"))

        self.script.queues.update(
            lead=[reply("", call("delegate_task", assignee="writer", task="Write release notes for v1.",
                                 deliverables=["RELEASE_NOTES.md"], kind="adapt", background=True)),
                  lead_second,
                  lambda m: (seen.__setitem__("waited", tool_results(m)[-1]), reply("shipped"))[1]],
            writer=[writer_first, reply("Release notes written.")])
        self.tid = self.studio.post(self.gid, "Release v1", queue=True)["task_id"]
        wait_for(lambda: self.studio.group(self.gid).load_task(self.tid)["status"] == "paused")
        self.assertTrue(path_for(self.studio.group(self.gid), self.tid).is_file())
        self.studio.close()
        self.open()
        self.studio.resume_task(self.tid)
        wait_for(lambda: self.studio.group(self.gid).load_task(self.tid)["status"] == "completed", timeout=15)
        store = self.studio.group(self.gid)
        record = store.load_task(self.tid)
        self.assertEqual(record["final_answer"], "shipped")
        self.assertIn("Release notes written.", seen["waited"])
        self.assertEqual(len(self.provider.for_agent("writer")), 2, "the writer continued from its pause frame")
        self.assertEqual((store.project.root / "RELEASE_NOTES.md").read_text(), "v1 notes\n")
        kinds = [e["kind"] for e in store.chat() if e.get("task_id") == self.tid]
        self.assertEqual((kinds.count("task_paused"), kinds.count("task_resumed"), kinds.count("report")), (1, 1, 1))


    def test_pause_after_the_lead_answered_keeps_its_wait_and_resumes_it(self):
        answered, seen = threading.Event(), {}

        def lead_second(m):
            answered.set()
            return reply("first answer")

        def writer_first(m):
            answered.wait(5)
            time.sleep(0.3)  # the lead is now waiting for the hand-off
            self.assertEqual(self.studio.pause_task(self.tid)["status"], "pausing")
            return reply("", call("write_file", path="RELEASE_NOTES.md", content="v1 notes\n"))

        self.script.queues.update(
            lead=[reply("", call("delegate_task", assignee="writer", task="Write release notes for v1.",
                                 deliverables=["RELEASE_NOTES.md"], kind="adapt", background=True)),
                  lead_second,
                  lambda m: (seen.__setitem__("waited", tool_results(m)[-1]), reply("final with notes"))[1]],
            writer=[writer_first, reply("Release notes written.")])
        self.tid = self.studio.post(self.gid, "Release v1", queue=True)["task_id"]
        wait_for(lambda: self.studio.group(self.gid).load_task(self.tid)["status"] == "paused")
        self.studio.close()
        self.open()
        self.studio.resume_task(self.tid)
        wait_for(lambda: self.studio.group(self.gid).load_task(self.tid)["status"] == "completed", timeout=15)
        store = self.studio.group(self.gid)
        self.assertEqual(store.load_task(self.tid)["final_answer"], "final with notes")
        self.assertIn("Release notes written.", seen["waited"])
        self.assertEqual(len(self.provider.for_agent("lead")), 3, "the lead did not start over after the resume")
        self.assertEqual([e["text"] for e in store.chat() if e["kind"] == "final"], ["final with notes"])


if __name__ == "__main__":
    unittest.main()
