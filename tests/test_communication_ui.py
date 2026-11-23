"""A real browser addresses a running member, then reopens the durable conversation."""
import tempfile
import unittest
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from miniagent.server import serve
from miniagent.studio import Studio
from tests.helpers import FakeProvider, Script, call, reply, settings
from tests.test_team import TEAM, finished, wait_for


class CommunicationUITest(unittest.TestCase):
    def test_address_interrupt_receipt_and_folded_peer_answer_survive_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = "@dev 改为交付周二版"
            script = Script(lead=[reply("", call("delegate_task", assignee="dev", task="Check the release reference.")),
                                  reply("Release recorded.")],
                            dev=[reply("", call("ask_teammate", member="qa", question="Which release is signed?")),
                                 reply("", call("run_shell", command="sleep 30")),
                                 reply("Updated the release.")],
                            qa=[reply("R42 is signed.")],
                            room=[reply("", call("change_current_task", quote=text))])
            provider = FakeProvider(script)
            cfg = settings(Path(tmp))
            studio = Studio(cfg, provider_factory=lambda _: provider)
            gid = studio.create_group("Release", TEAM, "lead", "autonomous")["id"]
            main, preview = serve(studio, 0)
            try:
                chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
                with sync_playwright() as pw, pw.chromium.launch(**({"executable_path": str(chrome)}
                                                                     if chrome.exists() else {})) as browser:
                    page = browser.new_page()
                    page.goto(f"http://127.0.0.1:{main.server_port}/#{gid}")
                    studio.post(gid, "Check the release", queue=True)
                    def started():
                        live = studio.state(gid)["live"] or {}
                        return any(e.get("tool") == "run_shell" for e in live.get("activity", []))
                    wait_for(started)
                    field = page.locator("#input")
                    field.fill(text)
                    expect(page.locator("#mail-recipient")).to_have_text("发给 @dev")
                    page.get_by_label("立即打断").check()
                    page.get_by_role("button", name="发送", exact=True).click()
                    receipt = page.locator(".msg.user").filter(has_text=text)
                    expect(receipt).to_have_count(1)
                    expect(receipt).to_contain_text("已打断")
                    self.assertEqual(finished(studio, gid)["status"], "completed")
                    page.reload()
                    expect(page.locator("#timeline .msg.user").filter(has_text=text)).to_contain_text("已打断")
                    question = page.locator('.team-mail[data-kind="teammate_question"] details')
                    expect(question).to_have_count(1)
                    self.assertFalse(question.evaluate("node => node.open"))
                    question.locator("summary").click()
                    expect(question).to_contain_text("R42 is signed.")
                    expect(page.get_by_label("立即打断")).to_be_hidden()
            finally:
                studio.close()
                for server in (main, preview):
                    server.shutdown()
                    server.server_close()
