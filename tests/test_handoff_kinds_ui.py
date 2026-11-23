"""A real browser shows each hand-off's kind and whether it ran in the background."""
import tempfile
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
                                     deliverables=["RELEASE_NOTES.md"], kind="adapt")),
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
                    page = browser.new_page()
                    page.goto(f"http://127.0.0.1:{main.server_port}/#{gid}")
                    cards = page.locator(".delegation")
                    expect(cards).to_have_count(2)
                    expect(cards.nth(0).locator(".checks-count")).to_have_text(["制作"])
                    expect(cards.nth(1).locator(".checks-count")).to_have_text(["改写", "后台"])
                    reports = page.locator(".report")
                    expect(reports).to_have_count(2)
                    expect(reports.filter(has_text="notes written").locator(".checks-count")).to_have_text(["后台"])
                    expect(reports.filter(has_text="built").locator(".checks-count")).to_have_count(0)
            finally:
                for server in (main, preview):
                    server.shutdown()
                    server.server_close()
                studio.close()


if __name__ == "__main__":
    unittest.main()
