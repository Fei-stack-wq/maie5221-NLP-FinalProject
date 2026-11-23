"""Offline conversations through the same Studio.post used by the browser."""
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from miniagent.studio import Studio, StudioError
from miniagent.studio_tools import assistant_tools
from tests.helpers import FakeProvider, Script, call, reply, settings
from tests.test_room_turn import action, evidence, file_hashes
from tests.test_team import wait_for


class AssistantRoomTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.script = Script()
        self.provider = FakeProvider(self.script)
        self.studio = Studio(settings(Path(tmp.name)), lambda _:self.provider)
        self.addCleanup(self.studio.close)
        self.aid = self.studio.assistant()["id"]
        for key in ("editor","reviewer","analyst"):
            self.studio.roles.save({"id":key,"name":key.title(),"prompt":"Work as "+key})
        self.team = self.studio.create_group("Desk", [], "editor", role_ids=["editor"])["id"]
        self.dm = self.studio.create_group("Private", [], "analyst", role_ids=["analyst"], kind="dm")["id"]

    def message(self, text, *responses):
        self.script.queues["room"] = list(responses)
        return self.studio.post(self.aid, text)

    def dispatched(self, target, text):
        self.script.queues["assistant"] = [reply("",call("start_task",group_id=target,text=text)),
                                           reply("Dispatched to the requested conversation.")]
        result = self.message(text, action("start_room_task",text))
        self.studio.wait(self.aid)
        return result["task_id"], self.studio.tasks(target)[-1]["id"]

    def test_existing_worker_can_lazy_load_tools_before_restart(self):
        calls = []
        def old_post(gid, text, *, queue=False):
            calls.append((gid,text,queue))
            return {"route":"task","task_id":"existing-worker"}
        service = SimpleNamespace(roles=self.studio.roles, group=self.studio.group,
                                  dispatch_status=self.studio.dispatch_status, post=old_post)
        tools = {t.name:t for t in assistant_tools(service)}
        self.assertNotIn("change_group_members",tools)
        result = tools["start_task"].fn(None, {"group_id":self.dm, "text":"Existing worker"})
        self.assertTrue(result.ok)
        self.assertEqual(calls,[(self.dm,"Existing worker",True)])

    def test_concurrent_roster_deltas_and_current_run_snapshot(self):
        entered, release = threading.Event(), threading.Event()
        def working(messages):
            entered.set()
            if not release.wait(8):
                raise AssertionError("test did not release model")
            return reply("",call("delegate_task",assignee="reviewer",task="Review this draft."))
        self.studio.change_members(self.team,add_role_ids=["reviewer"])
        self.script.queues.update(editor=[working,reply("Released."),reply("Next.")],
                                  reviewer=[reply("Existing assignment reviewed.")])
        self.studio.post(self.team,"Review the draft",queue=True)
        self.assertTrue(entered.wait(5))
        try:
            with ThreadPoolExecutor() as pool:
                jobs = [pool.submit(self.studio.change_members,self.team,**change)
                        for change in ({"add_role_ids":["analyst"]},{"remove_member_ids":["reviewer"]})]
                for job in jobs:
                    job.result()
            self.assertEqual({m.id for m in self.studio.group(self.team).group.members},{"editor","analyst"})
        finally:
            release.set()
        self.studio.wait(self.team)
        self.assertTrue(self.provider.for_agent("reviewer"))
        self.studio.post(self.team,"New subject",queue=True)
        self.studio.wait(self.team)
        self.assertIn("analyst",self.provider.for_agent("editor")[-1]["messages"][0]["content"])
        self.assertNotIn("reviewer",self.provider.for_agent("editor")[-1]["messages"][0]["content"])

