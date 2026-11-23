"""A task can set the thinking effort: from the composer, the API, or the Studio assistant when asked.

An explicit effort holds for every role that thinks, also against the hand-off kinds and fast
mode; without it Studio keeps its own setting and sizes the effort per hand-off.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from miniagent.assistant import SYSTEM
from miniagent.config import Limits, Settings
from miniagent.server import serve
from miniagent.studio import Studio, StudioError
from miniagent.studio_tools import TASK_OPTIONS
from tests.helpers import FakeProvider, Script, call, reply, settings
from tests.test_handoff_kinds import TEAM4
from tests.test_team import finished, wait_for


class EffortOptionTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.made: list[Settings] = []

    def studio(self, script):
        provider = FakeProvider(script)

        def factory(s):
            self.made.append(s)
            return provider
        studio = Studio(settings(self.tmp, limits=Limits(max_steps=150)), provider_factory=factory)
        self.addCleanup(studio.close)
        return studio, studio.create_group("Team", TEAM4, "lead", "autonomous")["id"], provider

    def kinds_script(self):
        return Script(lead=[reply("", call("delegate_task", assignee="dev", task="Build the export button.", kind="make")),
                            reply("", call("delegate_task", assignee="writer", task="Write the release notes.",
                                           kind="adapt", background=False)),
                            reply("done")],
                      dev=[reply("built")], writer=[reply("notes")])

    def test_an_explicit_effort_holds_for_every_role_that_thinks(self):
        studio, gid, provider = self.studio(self.kinds_script())
        studio.post(gid, "Ship it", queue=True, options={"reasoning_effort": "max"})
        task = finished(studio, gid)
        self.assertEqual(task["options"]["reasoning_effort"], "max")
        thinking = {s.reasoning_effort for s in self.made if s.thinking == "enabled"}
        self.assertEqual(thinking, {"max"}, "the lead and the make hand-off think at the chosen effort")
        self.assertIn("disabled", {s.thinking for s in self.made}, "adapt still works without thinking")
        self.assertNotIn("run_browser", provider.for_agent("writer")[0]["tools"], "the kind still sets the tools")

    def test_without_the_option_studio_keeps_its_setting_and_sizes_effort_per_hand_off(self):
        studio, gid, _ = self.studio(self.kinds_script())
        studio.post(gid, "Ship it", queue=True)
        task = finished(studio, gid)
        self.assertNotIn("reasoning_effort", task["options"])
        self.assertEqual({s.reasoning_effort for s in self.made if s.thinking == "enabled"}, {"high", "low"})

    def test_an_explicit_effort_wins_over_fast_modes_low(self):
        studio, gid, _ = self.studio(Script(lead=[reply("done")]))
        studio.post(gid, "Quick one", queue=True, options={"mode": "fast", "reasoning_effort": "high"})
        finished(studio, gid)
        lead = self.made[0]
        self.assertEqual((lead.model, lead.team.fast, lead.reasoning_effort), ("deepseek-flash", True, "high"))

    def test_an_unknown_effort_is_refused(self):
        studio, gid, _ = self.studio(Script())
        with self.assertRaisesRegex(StudioError, "reasoning_effort must be low, high or max"):
            studio.post(gid, "Ship it", options={"reasoning_effort": "extreme"})


class AssistantEffortTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.made: list[Settings] = []
        self.script = Script()
        provider = FakeProvider(self.script)

        def factory(s):
            self.made.append(s)
            return provider
        self.studio = Studio(settings(Path(tmp.name)), factory)
        self.addCleanup(self.studio.close)
        self.aid = self.studio.assistant()["id"]
        self.studio.roles.save({"id": "writer", "name": "Writer", "prompt": "Write short notes."})
        self.gid = self.studio.create_group("Desk", [], "writer", role_ids=["writer"])["id"]

    def test_the_assistant_passes_the_effort_the_user_asked_for(self):
        self.script.queues["assistant"] = [
            reply("", call("dispatch_task", group_id=self.gid, text="Write a memo about the booking system.",
                           options={"reasoning_effort": "max"})),
            reply("Sent to Desk with maximum thinking effort.")]
        self.script.queues["writer"] = [reply("Memo written.")]
        self.studio.post(self.aid, "让 Desk 写一份备忘录，思考强度调到最高。")
        task = wait_for(lambda: next((t for t in self.studio.tasks(self.gid) if t["status"] == "completed"), None))
        self.assertEqual(task["options"]["reasoning_effort"], "max")
        self.assertIn("max", {s.reasoning_effort for s in self.made})

    def test_the_rule_is_in_the_instructions_and_the_tool(self):
        self.assertIn('reasoning_effort ("low", "high", "max") in options only when the', SYSTEM)
        option = TASK_OPTIONS["properties"]["reasoning_effort"]
        self.assertEqual(option["enum"], ["low", "high", "max"])
        self.assertIn("Leave this out unless the requester asks", option["description"])


class ComposerEffortUITest(unittest.TestCase):
    def test_the_composer_sends_the_chosen_effort_and_disables_it_without_thinking(self):
        with tempfile.TemporaryDirectory() as tmp:
            provider = FakeProvider(Script(lead=[reply("first done"), reply("second done")],
                                           room=[reply("", call("start_room_task", quote="Build the export")),
                                                 reply("", call("start_room_task", quote="Build the import"))]))
            studio = Studio(settings(Path(tmp)), provider_factory=lambda _: provider)
            gid = studio.create_group("Team", TEAM4, "lead", "autonomous")["id"]
            main, preview = serve(studio, 0)
            try:
                chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
                with sync_playwright() as pw, pw.chromium.launch(**({"executable_path": str(chrome)}
                                                                     if chrome.exists() else {})) as browser:
                    page = browser.new_page()
                    page.goto(f"http://127.0.0.1:{main.server_port}/#{gid}")
                    page.get_by_label("思考强度").select_option("max")
                    page.locator("#input").fill("Build the export")
                    page.get_by_role("button", name="发送", exact=True).click()
                    first = finished(studio, gid)
                    self.assertEqual(first["options"]["reasoning_effort"], "max")
                    expect(page.get_by_label("思考强度")).to_have_value("", timeout=5000)
                    page.get_by_label("思考强度").select_option("high")
                    page.get_by_label("思考模式").select_option("disabled")
                    expect(page.get_by_label("思考强度")).to_be_disabled()
                    expect(page.get_by_label("思考强度")).to_have_value("")
                    page.locator("#input").fill("Build the import")
                    page.get_by_role("button", name="发送", exact=True).click()
                    wait_for(lambda: len([t for t in studio.tasks(gid) if t["status"] == "completed"]) == 2)
                    second = studio.tasks(gid)[-1]
                    self.assertEqual(second["options"]["thinking"], "disabled")
                    self.assertNotIn("reasoning_effort", second["options"])
            finally:
                for server in (main, preview):
                    server.shutdown()
                    server.server_close()
                studio.close()


if __name__ == "__main__":
    unittest.main()
