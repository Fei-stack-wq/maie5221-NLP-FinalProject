"""A real browser shows each hand-off's kind live while running and in the task tree."""
import tempfile
import threading
import unittest
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from miniagent.server import serve
from miniagent.studio import Studio
from tests.helpers import FakeProvider, Script, call, reply, settings
from tests.test_handoff_kinds import TEAM4
from tests.test_team import finished


class HandoffKindsUITest(unittest.TestCase):
    def test_delegation_cards_show_the_kind_and_background_chips(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Script(
                lead=[reply("", call("delegate_task", assignee="dev", task="Build the export button.",
                                     deliverables=["app.py"], kind="make")),
                      reply("", call("delegate_task", assignee="writer", task="Write the release notes.",
                                     deliverables=["RELEASE_NOTES.md"], kind="adapt", background=True)),
                      reply("", call("wait_handoffs")), reply("Shipped.")],
                dev=[reply("built")], writer=[reply("notes written")])
            provider = FakeProvider(script)
            studio = Studio(settings(Path(tmp)), provider_factory=lambda _: provider)
            gid = studio.create_group("Release", TEAM4, "lead", "autonomous")["id"]
            studio.post(gid, "Ship the export", queue=True)
            self.assertEqual(finished(studio, gid)["status"], "completed")
            main, preview = serve(studio, 0)
            try:
                chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
                with sync_playwright() as pw, pw.chromium.launch(**({"executable_path": str(chrome)}
                                                                     if chrome.exists() else {})) as browser:
                    page = browser.new_page(viewport={"width": 1280, "height": 850})
                    errors = []
                    page.on("pageerror", lambda err: errors.append(str(err)))
                    page.goto(f"http://127.0.0.1:{main.server_port}/#{gid}")
                    cards = page.locator(".delegation")
                    expect(cards).to_have_count(2)
                    expect(cards.nth(0).locator(".checks-count")).to_have_text(["制作"])
                    expect(cards.nth(1).locator(".checks-count")).to_have_text(["改写", "后台"])
                    reports = page.locator(".report")
                    expect(reports).to_have_count(2)
                    expect(reports.filter(has_text="notes written").locator(".checks-count")).to_have_text(["后台"])
                    expect(reports.filter(has_text="built").locator(".checks-count")).to_have_count(0)
                    expect(reports.filter(has_text="built").locator(".kind-badge")).to_have_text("制作 make")
                    expect(reports.filter(has_text="notes written").locator(".kind-badge")).to_have_text("改写 adapt")

                    page.locator(".chat-run").get_by_role("button", name="查看任务树").click()
                    expect(page.locator(".tree-root .tree-root-kinds .kind-badge")).to_have_text(["制作 make", "改写 adapt"])
                    branches = page.locator(".task-tree .branch")
                    expect(branches).to_have_count(2)
                    expect(branches.nth(0)).to_have_attribute("data-handoff-kind", "make")
                    expect(branches.nth(0).locator(".tree-kind")).to_have_text("制作 make")
                    expect(branches.nth(1)).to_have_attribute("data-handoff-kind", "adapt")
                    expect(branches.nth(1).locator(".tree-kind")).to_have_text("改写 adapt")
                    expect(branches.nth(1).locator(".tree-bg-badge")).to_have_text("后台")
                    page.locator("#sheet-close").click()

                    page.locator("#workbench-nav").click()
                    row = page.locator(".bench-row").filter(has_text="Ship the export")
                    expect(row.locator(".bench-kinds .kind-badge")).to_have_text(["制作 make", "改写 adapt"])
                    self.assertEqual(errors, [])
            finally:
                for server in (main, preview):
                    server.shutdown()
                    server.server_close()
                studio.close()

    def test_live_status_members_and_tree_show_running_role_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            qa_started, writer_started, release = threading.Event(), threading.Event(), threading.Event()

            def writer_step(_m):
                writer_started.set()
                release.wait(10)
                return reply("Notes drafted.")

            def qa_step(_m):
                qa_started.set()
                release.wait(10)
                return reply("VERDICT: APPROVED")

            script = Script(
                lead=[
                    reply("", call("delegate_task", assignee="writer", task="Draft the release summary.",
                                   deliverables=["NOTES.md"], kind="adapt", background=True)),
                    reply("", call("delegate_task", assignee="qa", task="Verify the release end to end.",
                                   deliverables=["QA.md"], kind="check")),
                    reply("", call("wait_handoffs")),
                    reply("Verified and documented."),
                ],
                writer=[writer_step],
                qa=[qa_step],
            )
            provider = FakeProvider(script)
            studio = Studio(settings(Path(tmp)), provider_factory=lambda _: provider)
            gid = studio.create_group("Release", TEAM4, "lead", "autonomous")["id"]
            main, preview = serve(studio, 0)
            try:
                studio.post(gid, "Verify and write release notes", queue=True)
                self.assertTrue(writer_started.wait(5))
                self.assertTrue(qa_started.wait(5))
                chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
                with sync_playwright() as pw, pw.chromium.launch(**({"executable_path": str(chrome)}
                                                                     if chrome.exists() else {})) as browser:
                    page = browser.new_page(viewport={"width": 1280, "height": 850})
                    errors = []
                    page.on("pageerror", lambda err: errors.append(str(err)))
                    page.goto(f"http://127.0.0.1:{main.server_port}/#{gid}")

                    live_badges = page.locator("#live-status .live-kind-badge")
                    expect(live_badges.filter(has_text="改写 adapt")).to_be_visible(timeout=5000)
                    expect(live_badges.filter(has_text="检查 check")).to_be_visible(timeout=5000)

                    qa_member = page.locator('#room-members li[data-member="qa"]')
                    writer_member = page.locator('#room-members li[data-member="writer"]')
                    expect(qa_member).to_have_class("working has-kind")
                    expect(qa_member.locator(".member-kind-badge")).to_have_text("检查 check")
                    expect(writer_member).to_have_class("working has-kind")
                    expect(writer_member.locator(".member-kind-badge")).to_have_text("改写 adapt")

                    page.locator("#toggle-activity").click()
                    expect(page.locator("#activity")).to_contain_text("类型：检查 (check)")
                    expect(page.locator("#activity")).to_contain_text("类型：改写 (adapt)")

                    page.locator(".chat-run").get_by_role("button", name="查看任务树").click()
                    branches = page.locator(".task-tree .branch")
                    expect(branches).to_have_count(2)
                    expect(branches.nth(0).locator(".tree-kind")).to_have_text("改写 adapt")
                    expect(branches.nth(0).locator(".tree-status")).to_have_text("等待回报")
                    expect(branches.nth(1).locator(".tree-kind")).to_have_text("检查 check")
                    expect(branches.nth(1).locator(".tree-status")).to_have_text("等待回报")

                    release.set()
                    expect(branches.nth(0).locator(".status")).to_have_text("完成", timeout=10000)
                    expect(branches.nth(1).locator(".status")).to_have_text("完成", timeout=10000)
                    expect(branches.nth(0).locator(".tree-kind")).to_have_text("改写 adapt")
                    expect(branches.nth(1).locator(".tree-kind")).to_have_text("检查 check")
                    self.assertEqual(errors, [])
            finally:
                release.set()
                for server in (main, preview):
                    server.shutdown()
                    server.server_close()
                studio.close()


if __name__ == "__main__":
    unittest.main()
