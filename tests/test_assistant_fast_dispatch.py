"""The Studio assistant dispatches in standard mode unless the user asks it for fast mode."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from miniagent.assistant import SYSTEM
from miniagent.studio import Studio
from tests.helpers import FakeProvider, Script, call, reply, settings
from tests.test_team import wait_for


class AssistantFastDispatchTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.created = []
        self.script = Script()
        self.provider = FakeProvider(self.script)

        def factory(s):
            self.created.append(s.team.fast)
            return self.provider

        self.studio = Studio(settings(Path(tmp.name)), factory)
        self.addCleanup(self.studio.close)
        self.aid = self.studio.assistant()["id"]
        self.studio.roles.save({"id": "writer", "name": "Writer", "prompt": "Write short notes."})
        self.gid = self.studio.create_group("Desk", [], "writer", role_ids=["writer"])["id"]

    def dispatch(self, message, **options):
        self.script.queues["assistant"] = [
            reply("", call("dispatch_task", group_id=self.gid, text="Write a memo about the booking system.",
                           **({"options": options} if options else {}))),
            reply("Sent to Desk.")]
        self.script.queues["writer"] = [reply("Memo written.")]
        self.studio.post(self.aid, message)
        store = self.studio.group(self.gid)
        task = wait_for(lambda: next((t for t in self.studio.tasks(self.gid) if t["status"] == "completed"), None))
        started = next(e for e in store.chat() if e["kind"] == "task_started" and e["task_id"] == task["id"])
        notice = next(e for e in self.studio.group(self.aid).chat() if e["kind"] == "dispatch_sent")
        return task, started, notice

    def test_dispatch_is_standard_by_default(self):
        task, started, notice = self.dispatch("Give Desk a memo to write.")
        self.assertNotIn("mode", task["options"])
        self.assertNotIn("fast", started)
        self.assertNotIn("fast", notice)
        self.assertNotIn(True, self.created, "no provider was made with fast settings")

    def test_fast_mode_when_the_user_asks_for_it(self):
        task, started, notice = self.dispatch("用快速模式让 Desk 写一份备忘录。", mode="fast")
        self.assertEqual(task["options"]["mode"], "fast")
        self.assertTrue(started["fast"])
        self.assertTrue(notice["fast"], "the receipt in the assistant chat shows fast mode")
        self.assertIn(True, self.created)

    def test_the_rule_is_in_the_assistant_instructions(self):
        self.assertIn('Set mode "fast" in options only when the user explicitly asks for', SYSTEM)
        self.assertIn("Never choose fast mode yourself", SYSTEM)


if __name__ == "__main__":
    unittest.main()
