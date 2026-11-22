import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

from miniagent.server import serve
from miniagent.studio import Studio
from tests.helpers import FakeProvider, Script, call, reply, settings
from tests.test_room_turn import action
from tests.test_team import wait_for


class DispatchNoticesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = settings(Path(tmp.name))
        self.script = Script()
        self.provider = FakeProvider(self.script)
        self.studio = Studio(self.settings, lambda _:self.provider)
        self.addCleanup(self.studio.close)
        self.aid = self.studio.assistant()["id"]
        self.source = self.studio.group(self.aid)
        self.studio.roles.save({"id":"writer","name":"Writer","prompt":"Write short notes."})
        self.gid = self.studio.create_group("Desk", [], "writer", role_ids=["writer"])["id"]

    def notices(self):
        return [e for e in self.source.chat() if e.get("notice_id")]

    def dispatch(self, mode="handoff", extra=()):
        text = "Give Desk a writing assignment."
        self.script.queues.update(room=[action("start_room_task",text)], assistant=[
            reply("",call("start_task",group_id=self.gid,text="Write a memo.",after_dispatch=mode)), *extra])
        return self.studio.post(self.aid,text)["task_id"]

    def reopen(self):
        self.studio.close()
        self.studio = Studio(self.settings, lambda _:self.provider)
        self.addCleanup(self.studio.close)
        self.source = self.studio.group(self.aid)
        self.studio.restore_queue()

    def test_queue_cancel_interruption_and_retry(self):
        self.script.queues["writer"] = [reply("",call("ask_human",question="Which audience?")),
                                        reply("First finished."),reply("",call("ask_human",question="Tone?"))]
        self.studio.post(self.gid,"An earlier task",queue=True)
        wait_for(lambda:(self.studio.state(self.gid)["live"] or {}).get("question"))
        parent = self.dispatch()
        self.studio.wait(self.aid)
        sent, = self.notices()
        self.assertTrue(sent["queued"])
        self.assertEqual(sent["task_id"],parent)
        self.studio.restore_queue()
        self.assertEqual(self.studio.group(self.gid).load_task(sent["target_task_id"])["status"],"queued")
        self.assertEqual(len(self.notices()),1)
        self.script.queues["room"] = [action("answer_open_question","Students")]
        self.studio.post(self.gid,"Students")
        wait_for(lambda:((self.studio.state(self.gid)["live"] or {}).get("question") or {}).get("question")=="Tone?")
        self.studio.cancel(self.gid)
        self.studio.wait(self.gid)
        self.assertEqual([e["kind"] for e in self.notices()],["dispatch_sent","dispatch_finished"])
        self.assertEqual(self.notices()[-1]["status"],"cancelled")
        self.assertEqual(self.notices()[-1]["target_task_id"], sent["target_task_id"])
        self.reopen()
        self.studio.restore_queue()
        self.assertEqual(len(self.notices()),2)
        target = self.studio.group(self.gid)
        tid="t-abc123def456"
        target.save_task({"id":tid,"group_id":self.gid,"status":"running","text":"Lost worker",
                          "dispatched_by":{"group_id":self.aid,"task_id":parent}})
        self.reopen()
        self.assertEqual([e["status"] for e in self.notices() if e["kind"]=="dispatch_finished"],["cancelled","interrupted"])
        self.assertEqual(len({e["notice_id"] for e in self.notices()}),4)

    def test_await_result_uses_completion_signal_and_reports_failed_start(self):
        self.script.queues["writer"] = [reply("Delivery in the other room.")]
        def synthesize(messages):
            result = json.loads(messages[-1]["content"])
            self.assertEqual(result["final_answer"],"Delivery in the other room.")
            self.assertEqual(self.notices()[-1]["status"],"completed")
            return reply("Combined the result.")
        parent = self.dispatch("await_result",[synthesize])
        self.studio.wait(self.aid)
        self.assertEqual(self.source.load_task(parent)["final_answer"],"Combined the result.")
        self.assertEqual(len(self.provider.for_agent("assistant")),2)
        with patch("miniagent.studio.sync",side_effect=ValueError("target workspace moved")):
            with self.assertRaisesRegex(Exception,"workspace moved"):
                self.studio.post(self.gid,"Launch another task",queue=True,
                                 dispatched_by={"group_id":self.aid,"task_id":parent})
        failed = self.notices()[-1]
        self.assertEqual(failed["status"],"failed")
        self.assertIn("workspace moved", failed["error"])
        self.assertEqual(failed["artifacts"],[])
        self.assertEqual([e["kind"] for e in self.notices()][-2:],["dispatch_sent","dispatch_finished"])

    def test_continue_and_partial_tool_batch_preserve_independent_work(self):
        self.script.queues["writer"] = [reply("First delivered"),reply("Second delivered")]
        parent = self.dispatch("continue",[reply("",call("write_file",path="own.md",content="Independent work")),
                                           reply("Finished my own part.")])
        self.studio.wait(self.aid)
        self.assertEqual(self.source.load_task(parent)["status"],"completed")
        self.assertTrue((self.source.project.root / "own.md").is_file())
        self.script.queues["assistant"] = [
            reply("",call("start_task",group_id=self.gid,text="Write again"),call("start_task",group_id="unknown",text="Second job")),
            reply("First dispatched. The second room could not be found.")]
        result = self.studio.post(self.aid,"Two destinations",queue=True)
        self.studio.wait(self.aid)
        self.studio.wait(self.gid)
        self.assertIn("could not be found",self.source.load_task(result["task_id"])["final_answer"])
        self.assertEqual(len([e for e in self.notices() if e["kind"]=="dispatch_sent"]),2)

    def test_original_chat_links_live_result_and_versioned_artifact(self):
        gate = threading.Event()
        def write_when_released(messages):
            self.assertTrue(gate.wait(20))
            return reply("",call("write_file",path="memo.md",content="# Original memo"))
        def delegate(messages):
            gid = json.loads(messages[-1]["content"])["id"]
            return reply("",call("start_task",group_id=gid,text="Write memo.md."))
        text = "Create a team to write the memo."
        self.script.queues.update(room=[action("start_room_task",text)], assistant=[
            reply("",call("create_group",name="Writing Room",role_ids=["writer"],lead="writer")),delegate],
            writer=[write_when_released,reply("Delivered memo.md."),reply("",call("write_file",path="memo.md",content="# New memo")),reply("Updated.")])
        main,preview = serve(self.studio,0)
        base = "http://127.0.0.1:"+str(main.server_port)+"/"
        try:
            with sync_playwright() as pw:
                chrome=Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
                with pw.chromium.launch(**({"executable_path":str(chrome)} if chrome.exists() else {})) as browser:
                    page=browser.new_page()
                    errors=[]
                    page.on("pageerror",lambda error:errors.append(str(error)))
                    page.goto(base+"#"+self.aid)
                    page.locator("#input").fill(text)
                    page.locator("#composer button[type=submit]").click()
                    sent=page.locator('[data-notice="dispatch_sent"]')
                    created=page.locator('[data-notice="assistant_group_created"]')
                    returned=page.locator('[data-notice="dispatch_finished"]')
                    expect(created.get_by_role("link",name="Writing Room")).to_be_visible()
                    expect(sent.get_by_role("link",name="查看任务")).to_be_visible()
                    sent_event=self.notices()[1]
                    gid,tid=sent_event["target_group_id"],sent_event["target_task_id"]
                    task_link=sent.get_by_role("link",name="查看任务")
                    expect(task_link).to_have_attribute("href","#"+gid+"/task/"+tid)
                    expect(returned).to_have_count(0)
                    self.assertEqual(self.source.load_task(sent_event["task_id"])["status"],"dispatched")
                    task_link.click()
                    expect(page.locator("#sheet")).to_contain_text(tid)
                    page.locator("#sheet-close").click()
                    self.assertEqual(len(self.provider.for_agent("assistant")),2)
                    self.studio.restore_queue()
                    self.assertEqual(self.studio.group(gid).load_task(tid)["status"],"running")
                    from miniagent.metrics import summarize
                    summary = summarize([self.source.load_task(sent_event["task_id"]), {"status":"completed"}])
                    self.assertEqual((summary["dispatched"], summary["completion_rate"]), (1, 1.0))
                    self.assertEqual(self.studio.task_detail(sent_event["task_id"])["diagnostics"], [])
                    gate.set()
                    expect(returned).to_contain_text("Delivered memo.md.",timeout=10000)
                    self.studio.wait(gid)
                    self.studio.post(gid,"Update memo.md",queue=True)
                    self.studio.wait(gid)
                    page.reload()
                    expect(returned).to_have_count(1)
                    returned.get_by_role("button",name="memo.md").click()
                    expect(page.locator("#reader-body h1")).to_have_text("Original memo")
                    page.locator("#reader-close").click()
                    deep=browser.new_page()
                    deep.goto(base+task_link.get_attribute("href"))
                    expect(deep.locator("#sheet")).to_contain_text(tid)
                    sent.get_by_role("link",name="Writing Room").click()
                    expect(page.locator("#room-name")).to_contain_text("Writing Room")
                    self.assertEqual(errors,[])
                    self.assertEqual([e["kind"] for e in self.notices()],
                                     ["assistant_group_created","dispatch_sent","dispatch_finished"])
        finally:
            gate.set()
            for server in (main,preview):
                server.shutdown()
                server.server_close()
