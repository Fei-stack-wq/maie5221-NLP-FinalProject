import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from miniagent.server import serve
from miniagent.studio import Studio, StudioError
from tests.helpers import FakeProvider, Script, call, reply, settings


class InterruptedRecoveryTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        config = settings(Path(directory.name))
        self.script = Script()
        self.provider = FakeProvider(self.script)
        self.studio = Studio(config, lambda _: self.provider)
        self.addCleanup(self.studio.close)
        roles = [{"id":key,"name":key,"prompt":key} for key in ("lead","qa")]
        self.gid = self.studio.create_group("Software",roles,"lead")["id"]
        self.room = self.studio.group(self.gid)
        self.tid = "t-112233445566"
        event = self.room.append("user_message",task_id=self.tid,text="Build and release the booking app.")
        self.room.save_task({"id":self.tid, "status":"running","created_at":1, "primary":"lead",
                             "group_id":self.gid, "trigger_seq":event["seq"], "text":event["text"], "options":{},
                             "plan":{"steps":[{"step":"Build","status":"completed"},
                                              {"step":"Browser QA and release","status":"in_progress"}]}})
        self.room.append("delegation",task_id=self.tid,agent="lead",assignee="qa",task="Check browser behavior",
                         deliverables=["QA.md"])
        self.room.trace(self.tid,{"type":"tool_result","agent":"qa","tool":"run_browser","ok":False,"preview":"exit 1"})
        self.room.trace(self.tid,{"type":"step","agent":"qa","step":19})
        (self.room.project.root/"booking.html").write_text("Published app")
        (self.room.workspace("qa")/"screenshot.png").write_bytes(b"QA evidence")
        self.studio.restore_queue()

    def test_read_and_start_continue_existing_work_without_replaying_dev(self):
        self.script.queues["room"] = [
            reply("",call("read_room_evidence",workspace_path="booking.html"),call("start_room_task",quote="continue")),
            reply("",call("resume_room_task",quote="continue",task_id=self.tid)),
            reply("",call("start_room_task",quote="continue",task_id=self.tid))]
        self.script.queues["lead"] = [reply("",call("delegate_task",assignee="qa",task="Complete the pending browser QA and recover the screenshot.")),
                                      reply("Release complete.")]
        self.script.queues["qa"] = [reply("",call("copy_recovery_draft",path="screenshot.png",destination="verified.png")),
                                    reply("QA complete.")]
        result = self.studio.post(self.gid,"continue",client_id="msg1")
        self.studio.wait(self.gid,5)
        second = self.room.load_task(result["task_id"])
        self.assertEqual((second["status"],second["continued_from"],second["text"]),
                         ("completed",self.tid,"Build and release the booking app."))
        lead = self.provider.for_agent("lead")[0]
        for evidence in ("Browser QA and release","QA.md",'"step": 19',"booking.html"):
            self.assertIn(evidence,"\n".join(m["content"] for m in lead["messages"]))
        self.assertNotIn("copy_recovery_draft",lead["tools"])
        self.assertIn("copy_recovery_draft",self.provider.for_agent("qa")[0]["tools"])
        self.assertEqual((self.room.project.root/"verified.png").read_bytes(),b"QA evidence")
        self.assertFalse((self.room.project.root/"screenshot.png").exists())
        self.assertEqual((self.room.project.root/"booking.html").read_text(),"Published app")
        routes = self.provider.for_agent("room")
        self.assertIn("Published app",json.dumps(routes[1]["messages"]))
        self.assertFalse(any(m["role"] == "tool" for m in routes[2]["messages"]))
        self.assertEqual(self.studio.post(self.gid,"continue",client_id="msg1")["task_id"],second["id"])
        self.assertEqual(self.studio.recover_task(self.tid)["task_id"],second["id"])
        self.assertEqual(len(self.room.tasks()),2)
        self.assertEqual(self.room.load_task(self.tid)["status"],"interrupted")
        with self.assertRaises(StudioError):
            self.studio.recover_task(second["id"])

    def test_inquiry_and_independent_assignment_are_not_forced_into_recovery(self):
        self.script.queues["room"] = [reply("QA was interrupted."),reply("",call("start_room_task",quote="Another task",task_id="new"))]
        self.assertEqual(self.studio.post(self.gid,"What happened?")["route"],"reply")
        self.assertEqual(len(self.room.tasks()),1)
        self.script.queues["lead"] = [reply("Unrelated answer.")]
        new = self.studio.post(self.gid,"Another task")["task_id"]
        self.studio.wait(self.gid,5)
        self.assertNotIn("continued_from",self.room.load_task(new))
        self.assertEqual((self.room.task_dir(self.tid)/"private-drafts/qa/screenshot.png").read_bytes(),b"QA evidence")
        self.assertIsNone(self.studio.state(self.gid)["group"]["interrupted_task_id"])

    def test_racing_clicks_and_a_retry_after_restart_share_one_successor(self):
        self.script.queues["lead"] = [reply("Done.")]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self.studio.recover_task,[self.tid,self.tid]))
        self.assertEqual(results[0]["task_id"],results[1]["task_id"])
        self.studio.wait(self.gid,5)
        self.studio.close()
        restarted = Studio(self.studio.settings,lambda _:self.provider)
        self.addCleanup(restarted.close)
        restarted.restore_queue()
        self.assertEqual(restarted.recover_task(self.tid)["task_id"],results[0]["task_id"])
        self.assertEqual(len(self.provider.for_agent("lead")),1)

    def test_other_room_cannot_select_this_interruption(self):
        other = self.studio.create_session(self.gid,"Another brief")["id"]
        self.script.queues["room"] = [reply("",call("resume_room_task",quote="continue",task_id=self.tid))]
        self.assertEqual(self.studio.post(other,"continue")["route"],"reply")
        self.assertEqual(len(self.studio.group(other).tasks()),0)
        self.assertEqual(len(self.room.tasks()),1)

    def test_browser_play_creates_one_linked_run(self):
        self.script.queues["lead"] = [reply("Finished from saved work.")]
        main, preview = serve(self.studio,0)
        for server in (main,preview):
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        with sync_playwright() as pw:
            chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
            with pw.chromium.launch(**({"executable_path":str(chrome)} if chrome.exists() else {})) as browser:
                page = browser.new_page()
                page.goto(f"http://127.0.0.1:{main.server_port}/#{self.gid}")
                button = page.get_by_role("button",name="▶ 从已保存进度继续",exact=True)
                expect(button).to_be_visible()
                self.assertEqual(len(self.room.tasks()),1)
                button.click()
                expect(page.locator("#timeline")).to_contain_text("Finished from saved work.")
                expect(button).to_have_count(0)
                self.assertEqual([r["status"] for r in self.room.tasks()],["interrupted","completed"])
