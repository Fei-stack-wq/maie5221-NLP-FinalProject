"""The live plan is a projection of actual hand-offs, independently of the lead's decisions."""
import hashlib
import tempfile
import threading
import unittest
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from miniagent.config import fast_profile, load_settings
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

    def test_review_rounds_keep_the_step_and_do_not_change_the_leads_acceptance(self):
        self.script.queues.update(lead=[
            plan(step("Verify release", id="qa")),
            reply("", call("review_loop", author="dev", reviewer="qa", work="Inspect the release files.", step="qa")),
            plan(step("Verify release", "completed", id="qa")),
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

    def test_next_member_sees_the_report_and_files_without_another_plan_call(self):
        self.script.queues.update(lead=[plan(step("Write draft")),
            reply("", call("delegate_task", step="s1", assignee="dev", task="Write initial draft in note.md.")),
            reply("", call("delegate_task", assignee="qa", task="Read the available draft and report.")),
            reply("Ready.")], dev=[reply("", call("write_file", path="note.md", content="Hello")), reply("Ready.")],
            qa=[reply("Reviewed.")])
        self.studio.post(self.gid, "Prepare a note", queue=True)
        record = finished(self.studio, self.gid)
        context = self.provider.for_agent("qa")[0]["messages"][1]["content"]
        self.assertIn("[submitted] Write draft (ID: s1)", context)
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
        self.assertEqual((row["id"], row["stage"]), ("draft", "unconfirmed"))
        self.assertEqual([e["kind"] for e in row["history"]], ["delegation", "report"])
        self.assertEqual(view["counts"], {"total": 1, "accepted": 0, "in_progress": 0, "submitted": 1})

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
                    expect(card.locator(".plan-count")).to_have_text("进行中 1 · 已提交 0 · 已确认 0 / 1")
                    release.set()
                    self.assertTrue(submitted.wait(5))
                    expect(card.locator(".plan-count")).to_have_text("进行中 0 · 已提交 1 · 已确认 0 / 1")
                    digest = hashlib.sha256(b"Ready").hexdigest()
                    expect(card.get_by_role("link", name="note.md")).to_have_attribute("href", f"/api/tasks/{tid}/files/note.md?sha256={digest}")
                    expect(card).to_contain_text("检查 1/1")
                    finish.set()
                    expect(card.locator(".plan-stage")).to_have_text("已提交，尚未确认")
                    page.reload()
                    expect(card.locator(".plan-stage")).to_have_text("已提交，尚未确认")
                    expect(page.locator("#timeline .divider").filter(has_text="计划已更新")).to_have_text("计划已更新")
                    page.get_by_role("button", name="查看任务树").click()
                    expect(page.locator("#sheet .linked-plan .plan-count")).to_have_text("进行中 0 · 已提交 1 · 已确认 0 / 1")
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
