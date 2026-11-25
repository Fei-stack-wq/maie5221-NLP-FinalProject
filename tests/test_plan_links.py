"""The live plan is a projection of actual hand-offs, independently of the lead's decisions."""
import hashlib
import tempfile
import threading
import unittest
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from miniagent.config import fast_profile, load_settings
from miniagent.plan_view import plan_view
from miniagent.server import serve
from miniagent.studio import Studio
from tests.helpers import FakeProvider, Script, call, reply, settings, tool_results
from tests.test_team import TEAM, finished, wait_for


def step(text, status="pending", **extra):
    return {"step": text, "status": status, **extra}


def plan(*steps):
    return reply("", call("update_plan", steps=list(steps)))


class PlanLinksTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = settings(Path(tmp.name))
        self.script = Script()
        self.provider = FakeProvider(self.script)
        self.open()
        self.gid = self.studio.create_group("Software", TEAM, "lead", "interactive")["id"]

    def open(self):
        self.studio = Studio(self.settings, lambda _: self.provider)
        self.addCleanup(self.studio.close)

    def test_ids_survive_rewrites_and_bad_references_stay_unlinked(self):
        self.script.queues.update(lead=[
            plan(step("Build"), step("Test"), step("Review", id="s1")),
            plan(step("Test"), step("Ship"), step("Review", "rework", id="s1")),
            reply("", call("delegate_task", assignee="qa", step="s2", task="Inspect the release.")),
            lambda m: reply(tool_results(m)[-1])], qa=[reply("Inspected the release.")])
        self.studio.post(self.gid, "Prepare release", queue=True)
        record = finished(self.studio, self.gid)
        detail = self.studio.task_detail(record["id"], include_activity=False)
        before, after = [e for e in detail["events"] if e["kind"] == "plan"]
        self.assertEqual([r["id"] for r in before["steps"]], ["s2", "s3", "s1"])
        self.assertEqual([r["id"] for r in after["steps"]], ["s3", "s4", "s1"])
        self.assertEqual(record["delegations"], 1)
        self.assertIn("Valid step IDs: s3, s4, s1", record["final_answer"])
        self.assertEqual(detail["plan_view"]["unlinked"][0]["requested_step"], "s2")
        self.assertEqual(detail["plan_view"]["steps"][-1]["stage"], "rework")

    def test_review_rounds_confirm_the_step_without_a_second_plan_call(self):
        self.script.queues.update(lead=[
            plan(step("Verify release", id="qa")),
            reply("", call("review_loop", author="dev", reviewer="qa", work="Inspect the release files.", step="qa")),
            reply("Accepted.")],
            qa=[reply("VERDICT: CHANGES REQUESTED\nFix text."), reply("VERDICT: APPROVED")],
            dev=[reply("", call("write_file", path="fix.md", content="Fixed")), reply("Fixed.")])
        self.studio.post(self.gid, "Review the release", queue=True)
        record = finished(self.studio, self.gid)
        detail = self.studio.task_detail(record["id"])
        history = [e for e in detail["events"] if e["kind"] in ("delegation", "report")]
        self.assertEqual([(e["kind"], e["step"], e["loop"]["round"]) for e in history],
                         [(k, "qa", n) for n in (1, 1, 2) for k in ("delegation", "report")])
        view = detail["plan_view"]
        self.assertEqual([e["verdict"] for e in view["steps"][0]["history"] if "verdict" in e],
                         ["changes requested", "approved"])
        self.assertEqual(view["steps"][0]["evidence"]["reviewed"]["verdict"], "approved")
        self.assertEqual(view["counts"]["accepted"], 1)
        self.assertEqual(view["steps"][0]["decision"], {"stage":"completed", "source":"review", "agent":"qa"})
        self.assertEqual(sum(e["kind"] == "plan" for e in detail["events"]), 1)

    def test_lead_work_and_two_steps_share_one_retest(self):
        self.script.queues.update(lead=[
            plan(step("Design", id="design", lead_outputs=["design.md"]), step("Choose format", id="format"),
                 step("Fix", id="fix", owner="dev"), step("Retest", id="retest", owner="qa"),
                 step("Deliver notes", id="ship", on_delivery=True)),
            reply("", call("write_file", path="design.md", content="Format spec")),
            reply("", call("record_plan_result", step_ids=["format"], result="Selected CSV.")),
            reply("", call("review_loop", author="dev", reviewer="qa", work="Verify the format change.",
                           step="fix", retest_steps=["retest", "retest"])),
            reply("Shipped with release notes.")],
            qa=[reply("VERDICT: CHANGES REQUESTED\nCorrect the format."), reply("VERDICT: APPROVED")],
            dev=[reply("", call("write_file", path="fix.md", content="Corrected")), reply("Fixed.")])
        tid = self.studio.post(self.gid, "Ship the change", queue=True)["task_id"]
        finished(self.studio, self.gid)
        detail = self.studio.task_detail(tid)
        rows = {s["id"]: s for s in detail["plan_view"]["steps"]}
        self.assertEqual(detail["plan_view"]["counts"]["completed"], 5)
        self.assertEqual({k for k, s in rows.items() if s["confirmed"]}, {"fix", "retest"})
        self.assertEqual(rows["retest"]["history"], rows["fix"]["history"][-2:])
        self.assertEqual(rows["design"]["evidence"]["lead_work"]["files"][0]["path"], "design.md")
        self.assertEqual(rows["format"]["evidence"]["lead_work"]["summary"], "Selected CSV.")
        self.assertEqual(rows["ship"]["evidence"]["lead_work"]["kind"], "final")
        self.assertEqual(sum(e["kind"] == "plan" for e in detail["events"]), 1)

    def test_lead_decides_one_step_without_replacing_the_plan(self):
        initial = [step("Build", id="build", owner="dev"), step("Ship", id="ship")]
        self.script.queues.update(lead=[plan(*initial),
            reply("", call("delegate_task", assignee="dev", step_ids=["build"], task="Prepare the build.")),
            reply("", call("record_plan_result", step_ids=["build"], decision="rework", result="Fix format.")),
            reply("", call("delegate_task", assignee="dev", step_ids=["build"], task="Correct the format.")),
            reply("", call("record_plan_result", step_ids=["build"], decision="accept", result="Verified fix.")),
            reply("Ready.")], dev=[reply("Built."), reply("Fixed.")])
        tid = self.studio.post(self.gid, "Prepare build", queue=True)["task_id"]
        finished(self.studio, self.gid)
        detail = self.studio.task_detail(tid)
        self.assertEqual(sum(e["kind"] == "plan" for e in detail["events"]), 1)
        self.assertEqual(detail["plan_view"]["counts"]["completed"], 1)
        built, ship = detail["plan_view"]["steps"]
        self.assertTrue(built["confirmed"])
        self.assertFalse(ship["completed"])
        self.assertEqual([e["status"] for e in built["history"] if e["kind"] == "plan_decision"],
                         ["rework", "completed"])
        self.assertEqual(built["decision"]["source"], "lead")

    def test_historical_links_are_atomic_scoped_and_idempotent(self):
        from miniagent.studio import StudioError
        self.script.queues.update(lead=[plan(step("Design", id="design"), step("Repair", id="fix"),
                                             step("Verify", id="retest"), step("Release", id="ship")),
            reply("", call("write_file", path="design.md", content="Spec")),
            reply("", call("delegate_task", assignee="qa", step="fix", review=True, task="Verify repaired build.")),
            reply("Delivered.")], qa=[reply("VERDICT: APPROVED")])
        tid = self.studio.post(self.gid, "Ship it", queue=True)["task_id"]
        finished(self.studio, self.gid)
        detail = self.studio.task_detail(tid)
        seq = {e["kind"]: e["seq"] for e in detail["events"]}
        links = [{"source_seq": seq[kind], "step_ids": [key]} for kind, key in
                 (("publish", "design"), ("report", "retest"), ("final", "ship"))]
        with self.assertRaises(StudioError):
            self.studio.link_plan_evidence(tid, links + [{"source_seq": seq["plan"], "step_ids": ["ship"]}])
        self.assertEqual(self.studio.task_detail(tid)["events"], detail["events"])
        self.assertEqual(self.studio.link_plan_evidence(tid, [{"source_seq": seq["report"], "step_ids": ["fix"]}]),
                         {"links_added": 0})
        self.assertEqual(self.studio.link_plan_evidence(tid, links)["links_added"], 3)
        self.assertEqual(self.studio.link_plan_evidence(tid, links)["links_added"], 0)
        after = self.studio.task_detail(tid)
        self.assertEqual(after["events"][:-1], detail["events"])
        self.assertEqual(after["task"], detail["task"])
        self.assertEqual((after["plan_view"]["counts"]["completed"], after["plan_view"]["counts"]["accepted"]), (4, 2))

    def test_signed_step_does_not_confirm_other_work_or_survive_reassignment(self):
        steps = [step("Fix build", id="fix"), step("Release", "in_progress", id="ship")]
        record = {"id":"t", "primary":"lead", "status":"completed", "plan":{"linked":True,"steps":steps}}
        events = []

        def append(kind, **fields):
            seq = len(events) + 1
            events.append({"task_id":"t", "seq":seq, "time":seq, "kind":kind, **fields})
            return seq

        def view():
            return plan_view(record, events)

        def assign():
            return append("delegation", assignee="qa", task="Review the build", review=True, step="fix")

        def report(seq, verdict, **fields):
            append("report", agent="qa", status="completed", review=True, step="fix",
                   assignment_seq=seq, verdict=verdict, **fields)

        append("plan", agent="lead", steps=steps)
        report(assign(), "changes requested")
        self.assertEqual(view()["steps"][0]["stage"], "rework")
        report(assign(), "approved")
        append("plan", agent="lead", steps=steps)
        self.assertEqual([s["stage"] for s in view()["steps"]], ["completed","unrecorded"])
        self.assertEqual(view()["counts"]["accepted"], 1)
        report(assign(), "unclear")
        self.assertEqual(view()["counts"]["accepted"], 0)
        report(assign(), "approved", text="**VERDICT: APPROVED WITH LISTED FIXES** — see report.")
        self.assertEqual(view()["steps"][0]["evidence"]["reviewed"]["verdict"], "unclear")
        self.assertEqual(view()["counts"]["accepted"], 0)
        report(assign(), "approved", checks={"passed":0,"total":1})
        self.assertEqual(view()["counts"]["accepted"], 0)
        for problem in ({"missing_deliverables": ["required.md"]}, {"unpublished_files": ["blocked.md"]}):
            report(assign(), "approved", **problem)
            self.assertFalse(view()["steps"][0]["completed"])
            self.assertFalse(view()["steps"][0]["confirmed"])
        old = assign()
        newer = append("delegation", assignee="dev", task="Change build", step="fix")
        append("report", agent="dev", status="completed", assignment_seq=newer, step="fix")
        report(old, "approved")
        self.assertEqual(view()["counts"]["accepted"], 0)
        report(assign(), "approved")
        record["plan"]["steps"] = [step("Fix build","rework", id="fix"), steps[1]]
        append("plan", agent="lead", steps=record["plan"]["steps"])
        self.assertEqual(view()["steps"][0]["decision"], {"stage":"rework", "source":"lead", "agent":"lead"})

    def test_production_counts_while_awaiting_review_and_reopen_invalidates_it(self):
        steps = [step("Build", id="build")]
        record = {"id": "t", "primary": "lead", "status": "running", "plan": {"linked": True, "steps": steps}}
        events = []
        def append(kind, **data):
            seq = len(events) + 1
            events.append(dict(kind=kind, task_id="t", time=seq, seq=seq, **data))
            return seq
        def check(done, signed):
            row = plan_view(record, events)["steps"][0]
            self.assertEqual((row["completed"], row["confirmed"]), (done, signed))
        append("plan", agent="lead", steps=steps)
        assigned = append("delegation", assignee="dev", task="Build", step="build")
        append("report", agent="dev", status="completed", step="build", assignment_seq=assigned)
        qa = append("delegation", assignee="qa", task="Verify", step="build", review=True)
        check(True, False)
        append("report", agent="qa", status="completed", assignment_seq=qa, verdict="approved", review=True)
        check(True, True)
        record["plan"]["steps"] = [step("Build", "in_progress", id="build")]
        append("plan", agent="lead", steps=record["plan"]["steps"])
        check(False, False)

    def test_publication_must_cover_all_outputs_and_deletion_reopens(self):
        steps = [step("Design", id="spec", owner="lead", lead_outputs=["api.md", "data.md"]),
                 step("Legacy", id="legacy", owner="lead", outputs=["old.md"]),
                 step("Other", id="other", owner="lead")]
        record = {"id":"t", "primary":"lead", "status":"completed", "plan":{"linked":True, "steps":steps}}
        events = []
        def publish(path, change):
            seq = len(events) + 1
            events.append(dict(kind="publish", task_id="t", time=seq, seq=seq, agent="lead",
                               changes=[dict(path=path, change=change)]))
            return [r["completed"] for r in plan_view(record, events)["steps"]]
        self.assertEqual(publish("api.md", "added"), [False, False, False])
        self.assertEqual(publish("data.md", "added"), [True, False, False])
        self.assertEqual(publish("api.md", "deleted"), [False, False, False])
        self.assertEqual(publish("api.md", "added"), [True, False, False])
        self.assertEqual(publish("old.md", "added"), [True, True, False])
        events.append(dict(kind="final", task_id="t", seq=6, time=6, agent="lead", status="completed", text="Done"))
        self.assertEqual(plan_view(record, events)["counts"]["completed"], 2)

    def test_qualified_review_returns_to_lead_without_accepting_or_ordering_a_fix(self):
        self.script.queues.update(lead=[
            plan(step("Check publication", id="check")),
            reply("", call("review_loop", author="dev", reviewer="qa", work="Check publication.", step="check")),
            lambda messages: reply(tool_results(messages)[-1])],
            qa=[reply("**VERDICT: APPROVED WITH LISTED FIXES** — apply edits first.")])
        self.studio.post(self.gid, "Review publication", queue=True)
        record = finished(self.studio, self.gid)
        detail = self.studio.task_detail(record["id"])
        self.assertEqual(record["delegations"], 1)
        self.assertIn("apply edits first", record["final_answer"])
        self.assertEqual(detail["plan_view"]["counts"]["accepted"], 0)
        self.assertEqual(detail["plan_view"]["steps"][0]["evidence"]["reviewed"]["verdict"], "unclear")

    def test_next_member_sees_the_report_and_files_without_another_plan_call(self):
        self.script.queues.update(lead=[plan(step("Write draft")),
            reply("", call("delegate_task", step="s1", assignee="dev", task="Write initial draft in note.md.")),
            reply("", call("delegate_task", assignee="qa", task="Read the available draft and report.")),
            reply("Ready.")], dev=[reply("", call("write_file", path="note.md", content="Hello")), reply("Ready.")],
            qa=[reply("Reviewed.")])
        self.studio.post(self.gid, "Prepare a note", queue=True)
        record = finished(self.studio, self.gid)
        context = self.provider.for_agent("qa")[0]["messages"][1]["content"]
        self.assertIn("[x] Write draft (ID: s1)", context)
        self.assertIn("submitted by dev: note.md", context)
        detail = self.studio.task_detail(record["id"])
        self.assertEqual(sum(e["kind"] == "plan" for e in detail["events"]), 1)
        self.assertEqual(detail["plan_view"]["steps"][0]["status"], "pending")

    def test_pause_reload_preserves_links_without_duplicate_handoffs(self):
        self.script.queues.update(lead=[plan(step("Draft notes", id="draft")),
            reply("", call("delegate_task", assignee="dev", task="Prepare a draft after checking the date.", step="draft")),
            reply("Ready.")], dev=[reply("", call("ask_human", question="Which date?")), reply("Draft ready.")])
        tid = self.studio.post(self.gid, "Draft the report", queue=True)["task_id"]
        wait_for(lambda: (self.studio.state(self.gid)["live"] or {}).get("question"))
        self.studio.pause_task(tid)
        self.studio.wait(self.gid, 5)
        self.assertEqual(self.studio.task_detail(tid)["task"]["status"], "paused")
        self.studio.close()
        self.open()
        self.studio.resume_task(tid)
        wait_for(lambda: (self.studio.state(self.gid)["live"] or {}).get("question"))
        self.script.queues["room"] = [reply("", call("answer_open_question", quote="Monday"))]
        self.studio.post(self.gid, "Monday")
        finished(self.studio, self.gid)
        view = self.studio.task_detail(tid)["plan_view"]
        row = view["steps"][0]
        self.assertEqual((row["id"], row["stage"]), ("draft", "completed"))
        self.assertEqual([e["kind"] for e in row["history"]], ["delegation", "report"])
        self.assertEqual(view["counts"], {"total": 1, "accepted": 0, "in_progress": 0, "submitted": 1, "completed": 1, "confirmed": 0})

    def test_live_fast_plan_and_final_history_in_browser(self):
        enter, release, submitted, finish = (threading.Event() for _ in range(4))
        def member(_):
            enter.set()
            release.wait(15)
            return reply("", call("write_file", path="note.md", content="Ready"))
        def coordinator(_):
            submitted.set()
            finish.wait(15)
            return reply("Delivered.")
        self.script.queues.update(lead=[plan(step("Draft", id="draft")),
            reply("", call("delegate_task", assignee="dev", step="draft", task="Write a release note.",
                           checks=[{"kind":"exists", "path":"note.md"}])), coordinator],
            dev=[member, reply("The note is available.")])
        main, preview = serve(self.studio, 0)
        try:
            tid = self.studio.post(self.gid, "Write a note", queue=True, options={"mode":"fast"})["task_id"]
            self.assertTrue(enter.wait(5))
            self.assertEqual(self.studio.state(self.gid)["live"]["plan"]["counts"]["in_progress"], 1)
            with sync_playwright() as pw:
                chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
                with pw.chromium.launch(**({"executable_path": str(chrome)} if chrome.exists() else {})) as browser:
                    page = browser.new_page()
                    page.goto(f"http://127.0.0.1:{main.server_port}/#{self.gid}")
                    card = page.locator("#timeline .linked-plan")
                    expect(card.locator(".plan-count")).to_have_text("已完成 0 / 1 · 已确认 0 / 1 · 进行中 1")
                    release.set()
                    self.assertTrue(submitted.wait(5))
                    expect(card.locator(".plan-count")).to_have_text("已完成 1 / 1 · 已确认 0 / 1 · 进行中 0")
                    digest = hashlib.sha256(b"Ready").hexdigest()
                    expect(card.get_by_role("link", name="note.md")).to_have_attribute("href", f"/api/tasks/{tid}/files/note.md?sha256={digest}")
                    expect(card).to_contain_text("检查 1/1")
                    finish.set()
                    expect(card.locator(".plan-stage")).to_have_text("已完成")
                    expect(card.locator(".plan-confirmation")).to_have_text("未确认")
                    page.reload()
                    expect(card.locator(".plan-stage")).to_have_text("已完成")
                    expect(card.locator(".plan-confirmation")).to_have_text("未确认")
                    expect(page.locator("#timeline .divider").filter(has_text="计划已更新")).to_have_text("计划已更新")
                    page.get_by_role("button", name="查看任务树").click()
                    expect(page.locator("#sheet .linked-plan .plan-count")).to_have_text("已完成 1 / 1 · 已确认 0 / 1 · 进行中 0")
            events = self.studio.task_detail(tid)["events"]
            self.assertEqual(sum(e["kind"] == "plan" for e in events), 1)
        finally:
            release.set()
            finish.set()
            for server in (main, preview):
                server.shutdown()
                server.server_close()

    def test_switch_restores_legacy_profile(self):
        path = self.settings.runs_dir / "plan-link-switch.env"
        path.write_text("MA_PLAN_LINKS=0\n")
        original = load_settings(path)
        self.assertFalse(original.team.plan_links)
        self.assertFalse(fast_profile(original).team.plans)

    def test_update_plan_demotes_in_progress_step_whose_prerequisite_is_unfinished(self):
        self.script.queues.update(lead=[
            plan(step("Research topic", "in_progress", id="s1", owner="dev"),
                 step("Draft brief", "in_progress", id="s2", after=[1])),
            reply("Done.")])
        tid = self.studio.post(self.gid, "Plan two dependent steps", queue=True)["task_id"]
        finished(self.studio, self.gid)
        detail = self.studio.task_detail(tid, include_activity=False)
        plan_event = next(e for e in detail["events"] if e["kind"] == "plan")
        self.assertEqual([(s["id"], s["status"]) for s in plan_event["steps"]],
                         [("s1", "in_progress"), ("s2", "pending")])

    def test_delegating_with_lead_published_input_closes_unmarked_lead_prerequisite(self):
        self.script.queues.update(lead=[
            plan(step("Write technical spec", "in_progress", id="s1"),
                 step("Implement booking app", "pending", id="s2", owner="dev", after=[1])),
            reply("", call("write_file", path="docs/SPEC.md", content="# Spec")),
            reply("", call("delegate_task", assignee="dev", step="s2", inputs=["docs/SPEC.md"],
                           task="Implement the app according to docs/SPEC.md.")),
            reply("Delivered.")],
            dev=[reply("Implemented.")])
        tid = self.studio.post(self.gid, "Build booking app", queue=True)["task_id"]
        finished(self.studio, self.gid)
        detail = self.studio.task_detail(tid, include_activity=False)
        s1, s2 = detail["plan_view"]["steps"]
        self.assertEqual(s1["stage"], "completed")
        self.assertTrue(s1["completed"])
        self.assertEqual(s1["evidence"]["lead_work"]["files"][0]["path"], "docs/SPEC.md")
        self.assertEqual(s2["stage"], "completed")

    def test_follow_up_with_overlapping_deliverable_auto_links_and_reopens_step(self):
        entered, release = threading.Event(), threading.Event()
        seen = {}

        def dev_rework(_m):
            entered.set()
            release.wait(10)
            return reply("", call("write_file", path="research/memo.md", content="# Memo v2"))

        self.script.queues.update(lead=[
            plan(step("Market research", id="research", owner="dev")),
            reply("", call("delegate_task", assignee="dev", step="research", kind="research",
                           deliverables=["research/memo.md"], task="Write the initial market research memo.")),
            reply("", call("record_plan_result", step_ids=["research"], decision="accept", result="Initial memo accepted.")),
            reply("", call("delegate_task", assignee="dev", kind="fix",
                           inputs=["research/memo.md"], deliverables=["research/memo.md"],
                           task="Patch missing citations in research/memo.md.")),
            lambda m: (seen.__setitem__("rework_result", tool_results(m)[-1]), reply("Delivered updated memo."))[1],
        ], dev=[
            reply("", call("write_file", path="research/memo.md", content="# Memo v1")),
            reply("Initial memo ready."),
            dev_rework,
            reply("Patched citations."),
        ])
        tid = self.studio.post(self.gid, "Prepare market memo", queue=True)["task_id"]
        try:
            self.assertTrue(entered.wait(5))
            live_plan = (self.studio.state(self.gid)["live"] or {}).get("plan")
            self.assertIsNotNone(live_plan)
            self.assertEqual(live_plan["counts"]["in_progress"], 1)
            self.assertEqual(live_plan["steps"][0]["stage"], "in_progress")
            self.assertEqual(live_plan["unlinked"], [])
        finally:
            release.set()
        finished(self.studio, self.gid)
        detail = self.studio.task_detail(tid, include_activity=False)
        delegations = [e for e in detail["events"] if e["kind"] == "delegation"]
        self.assertEqual(len(delegations), 2)
        self.assertEqual(delegations[1].get("step"), "research")
        self.assertTrue(delegations[1].get("inferred_step"))
        self.assertIn("automatically linked this follow-up to step 'research'", seen["rework_result"])
        step_row = detail["plan_view"]["steps"][0]
        self.assertEqual(step_row["stage"], "completed")
        self.assertFalse(step_row["confirmed"])
        self.assertEqual(detail["plan_view"]["unlinked"], [])

    def test_inputs_only_overlap_directory_and_ambiguous_deliverables_stay_unlinked(self):
        self.script.queues.update(lead=[
            plan(step("Research", id="research", owner="dev"),
                 step("Docs folder", id="docs", owner="dev"),
                 step("Shared A", id="s1", owner="dev"),
                 step("Shared B", id="s2", owner="dev")),
            reply("", call("delegate_task", assignee="dev", step="research",
                           deliverables=["research/memo.md"], task="Write research/memo.md.")),
            reply("", call("record_plan_result", step_ids=["research"], decision="accept", result="Research confirmed.")),
            reply("", call("delegate_task", assignee="dev", step="docs",
                           deliverables=["docs/"], task="Prepare files under docs/.")),
            reply("", call("delegate_task", assignee="dev", step="s1",
                           deliverables=["shared.md"], task="Draft shared.md part 1.")),
            reply("", call("delegate_task", assignee="dev", step="s2",
                           deliverables=["shared.md"], task="Draft shared.md part 2.")),
            # 1) Same assignee reads research/memo.md as input, writes a brand new deliverable: must NOT link to research.
            reply("", call("delegate_task", assignee="dev", inputs=["research/memo.md"],
                           deliverables=["slides/deck.md"], task="Build slides from research/memo.md.")),
            # 2) File inside a directory deliverable ('docs/'): exact path match must NOT falsely link to docs.
            reply("", call("delegate_task", assignee="dev", deliverables=["docs/extra.md"],
                           task="Add an extra note under docs/extra.md.")),
            # 3) Ambiguous deliverable touched by both s1 and s2: must NOT guess.
            reply("", call("delegate_task", assignee="dev", deliverables=["shared.md"],
                           task="Polish shared.md across sections.")),
            reply("All done."),
        ], dev=[
            reply("", call("write_file", path="research/memo.md", content="memo")), reply("memo done"),
            reply("docs done"),
            reply("s1 done"),
            reply("s2 done"),
            reply("deck done"),
            reply("extra done"),
            reply("shared done"),
        ])
        tid = self.studio.post(self.gid, "Check link boundaries", queue=True)["task_id"]
        finished(self.studio, self.gid)
        view = self.studio.task_detail(tid, include_activity=False)["plan_view"]
        rows = {s["id"]: s for s in view["steps"]}
        self.assertTrue(rows["research"]["confirmed"], "downstream input overlap must not reopen confirmed step")
        self.assertEqual(len(view["unlinked"]), 3)
        self.assertTrue(all(item["stage"] == "completed" and not item["active"] for item in view["unlinked"]))

    def test_running_unlinked_delegation_shows_active_state_in_plan_view_and_browser(self):
        entered, release = threading.Event(), threading.Event()

        def qa_adhoc(_m):
            entered.set()
            release.wait(10)
            return reply("Checked extra notes.")

        self.script.queues.update(lead=[
            plan(step("Draft", id="draft", owner="dev")),
            reply("", call("delegate_task", assignee="dev", step="draft", kind="make",
                           deliverables=["draft.md"], task="Write draft.md.")),
            reply("", call("delegate_task", assignee="qa", kind="research",
                           deliverables=["notes/adhoc.md"], task="Investigate an unscheduled side question.")),
            reply("Delivered with ad-hoc check."),
        ], dev=[
            reply("", call("write_file", path="draft.md", content="# Draft")),
            reply("Draft ready."),
        ], qa=[qa_adhoc])
        main, preview = serve(self.studio, 0)
        try:
            tid = self.studio.post(self.gid, "Draft and investigate", queue=True)["task_id"]
            self.assertTrue(entered.wait(5))
            live_plan = (self.studio.state(self.gid)["live"] or {}).get("plan")
            self.assertEqual(live_plan["counts"]["in_progress"], 0)
            self.assertEqual(len(live_plan["unlinked"]), 1)
            self.assertEqual(live_plan["unlinked"][0]["stage"], "in_progress")
            self.assertTrue(live_plan["unlinked"][0]["active"])
            with sync_playwright() as pw:
                chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
                with pw.chromium.launch(**({"executable_path": str(chrome)} if chrome.exists() else {})) as browser:
                    page = browser.new_page(viewport={"width": 1280, "height": 850})
                    errors = []
                    page.on("pageerror", lambda err: errors.append(str(err)))
                    page.goto(f"http://127.0.0.1:{main.server_port}/#{self.gid}")
                    card = page.locator("#timeline .linked-plan")
                    expect(card.locator(".plan-count")).to_have_text("已完成 1 / 1 · 已确认 0 / 1 · 进行中 0 · 计划外进行中 1")
                    unlinked_box = card.locator("details.unlinked")
                    expect(unlinked_box).to_have_attribute("open", "")
                    expect(unlinked_box.locator("summary")).to_have_text("其他派工（1 · 1 进行中）")
                    item = unlinked_box.locator(".unlinked-item.in_progress")
                    expect(item).to_contain_text("@qa")
                    expect(item.locator(".kind-badge")).to_have_text("调研 research")
                    expect(item.locator(".unlinked-stage")).to_have_text("进行中")

                    release.set()
                    expect(card.locator(".plan-count")).to_have_text("已完成 1 / 1 · 已确认 0 / 1 · 进行中 0", timeout=10000)
                    expect(unlinked_box.locator("summary")).to_have_text("其他派工（1）")
                    self.assertEqual(errors, [])
            final_view = self.studio.task_detail(tid, include_activity=False)["plan_view"]
            self.assertEqual(final_view["unlinked"][0]["stage"], "completed")
            self.assertFalse(final_view["unlinked"][0]["active"])
            self.assertEqual(final_view["unlinked"][0]["status"], "completed")
        finally:
            release.set()
            for server in (main, preview):
                server.shutdown()
                server.server_close()
