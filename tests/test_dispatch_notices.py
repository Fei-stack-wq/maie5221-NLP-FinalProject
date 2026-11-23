import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

from miniagent.server import serve
from miniagent.studio import Studio
from tests.helpers import FakeProvider, Script, call, reply, settings, tool_results
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

    def dispatch(self, *extra):
        """The assistant dispatches in one chat turn; returns that turn's ID (the receipts' origin)."""
        self.script.queues["assistant"] = [reply("",call("dispatch_task",group_id=self.gid,text="Write a memo.")),
                                           *(extra or [reply("Sent to Desk.")])]
        return self.studio.post(self.aid,"Give Desk a writing assignment.")["turn_id"]

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

    def test_outcome_reaches_the_next_turn_and_a_failed_start_is_reported(self):
        self.script.queues["writer"] = [reply("Delivery in the other room.")]
        parent = self.dispatch()
        wait_for(lambda:any(e["kind"]=="dispatch_finished" for e in self.notices()))
        self.assertEqual(self.notices()[-1]["status"],"completed")
        def synthesize(messages):
            self.assertIn("Delivery in the other room.",messages[0]["content"])
            return reply("Desk delivered the memo.")
        self.script.queues["assistant"] = [synthesize]
        self.assertEqual(self.studio.post(self.aid,"What did Desk deliver?")["text"],"Desk delivered the memo.")
        with patch("miniagent.studio.sync",side_effect=ValueError("target workspace moved")):
            with self.assertRaisesRegex(Exception,"workspace moved"):
                self.studio.post(self.gid,"Launch another task",queue=True,
                                 dispatched_by={"group_id":self.aid,"task_id":parent})
        failed = self.notices()[-1]
        self.assertEqual(failed["status"],"failed")
        self.assertIn("workspace moved", failed["error"])
        self.assertEqual(failed["artifacts"],[])
        self.assertEqual([e["kind"] for e in self.notices()][-2:],["dispatch_sent","dispatch_finished"])
    def test_partial_tool_batch_keeps_the_dispatch_that_succeeded(self):
        self.script.queues["writer"] = [reply("First delivered")]
        def explain(messages):
            results = tool_results(messages)
            self.assertIn("task_id",results[-2])
            self.assertTrue(results[-1].startswith("Error"))
            return reply("First dispatched. The second room could not be found.")
        self.script.queues["assistant"] = [
            reply("",call("dispatch_task",group_id=self.gid,text="Write again"),call("dispatch_task",group_id="unknown",text="Second job")),
            explain]
        result = self.studio.post(self.aid,"Two destinations")
        self.studio.wait(self.gid)
        self.assertIn("could not be found",result["text"])
        self.assertEqual(len([e for e in self.notices() if e["kind"]=="dispatch_sent"]),1)
        self.assertEqual(len(self.studio.tasks(self.gid)),1)
    def test_auto_pin_migrates_untouched_groups_and_respects_later_preferences(self):
        origin={"group_id":self.aid,"task_id":"t-000000000000"}
        new=self.studio.create_group("New",[],"writer",role_ids=["writer"],created_by=origin)
        private=self.studio.create_group("DM",[],"writer",role_ids=["writer"],kind="dm",created_by=origin)
        self.assertTrue(new["pinned"])
        self.assertFalse(private["pinned"])
        self.assertFalse(self.studio.summary(self.studio.group(self.gid))["pinned"])
        legacy=self.studio.group(new["id"])
        legacy.group.pinned=False
        self.studio.store.update_group(legacy)
        self.reopen()
        self.assertTrue(self.studio.group(new["id"]).group.pinned)
        self.studio.update_group(new["id"],{"pinned":False})
        self.reopen()
        self.assertFalse(self.studio.group(new["id"]).group.pinned)
        self.assertFalse(self.studio.group(private["id"]).group.pinned)

    def test_original_chat_links_live_result_and_versioned_artifact(self):
        gate = threading.Event()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        workspace = Path(tmp.name).resolve()/"前台工作区"
        workspace.mkdir()
        (workspace/"brief.txt").write_text("Front desk")
        def write_when_released(messages):
            self.assertTrue(gate.wait(20))
            return reply("",call("write_file",path="memo.md",content="# Original memo"))
        def delegate(messages):
            gid = json.loads(messages[-1]["content"])["id"]
            return reply("",call("dispatch_task",group_id=gid,text="Write memo.md."))
        text = "Create a team to write the memo."
        self.script.queues.update(assistant=[
            reply("",call("create_group",name="Writing Room",role_ids=["writer"],lead="writer")),delegate,
            reply("Created Writing Room and sent the memo.")],
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
                    choice = page.locator("#workspace-choice")
                    expect(choice).to_contain_text("Studio 默认目录")
                    choice.click()
                    expect(page.locator(".workspace-path")).to_have_text(str(self.source.project.root.resolve()))
                    choose = page.get_by_role("button",name="选择本地文件夹…")
                    with patch("miniagent.server.pick",return_value=""):
                        choose.click()
                        expect(choose).to_be_enabled()
                        expect(page.locator("#sheet")).to_be_visible()
                    with patch("miniagent.server.pick",return_value=str(workspace)):
                        choose.click()
                        expect(choice).to_contain_text(workspace.name)
                    page.locator("#input").fill(text)
                    page.locator("#composer button[type=submit]").click()
                    sent=page.locator('[data-notice="dispatch_sent"]')
                    created=page.locator('[data-notice="assistant_group_created"]')
                    returned=page.locator('[data-notice="dispatch_finished"]')
                    expect(created.get_by_role("link",name="Writing Room")).to_be_visible()
                    expect(sent.get_by_role("link",name="查看任务")).to_be_visible()
                    sent_event=self.notices()[1]
                    gid,tid=sent_event["target_group_id"],sent_event["target_task_id"]
                    pinned = page.locator("#group-list li").filter(has_text="Writing Room")
                    expect(pinned).to_be_visible()
                    target = self.studio.group(gid)
                    self.assertEqual(target.group.folder,str(workspace))
                    self.assertEqual((target.project.root/"brief.txt").read_text(),"Front desk")
                    task_link=sent.get_by_role("link",name="查看任务")
                    expect(task_link).to_have_attribute("href","#"+gid+"/task/"+tid)
                    expect(returned).to_have_count(0)
                    self.assertEqual(self.source.load_task(sent_event["task_id"])["status"],"completed")
                    task_link.click()
                    expect(page.locator("#sheet")).to_contain_text(tid)
                    page.locator("#sheet-close").click()
                    self.assertEqual(len(self.provider.for_agent("assistant")),3)
                    self.studio.restore_queue()
                    self.assertEqual(self.studio.group(gid).load_task(tid)["status"],"running")
                    gate.set()
                    expect(returned).to_contain_text("Delivered memo.md.",timeout=10000)
                    self.studio.wait(gid)
                    self.assertEqual((workspace/"memo.md").read_text(),"# Original memo")
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
                    expect(choice).to_have_attribute("title",str(workspace))
                    page.set_viewport_size({"width":390,"height":844})
                    pinned.scroll_into_view_if_needed()
                    expect(pinned).to_be_visible()
                    page.set_viewport_size({"width":1280,"height":860})
                    choice.click()
                    page.get_by_text("手动输入路径",exact=True).click()
                    page.get_by_label("文件夹路径").fill(str(workspace/"missing"))
                    page.get_by_role("button",name="使用此路径").click()
                    expect(page.locator("#sheet [role=alert]")).to_be_visible()
                    expect(choice).to_have_attribute("title",str(workspace))
                    page.get_by_role("button",name="使用 Studio 默认目录").click()
                    expect(choice).to_contain_text("Studio 默认目录")
                    self.assertEqual((workspace/"memo.md").read_text(),"# New memo")
                    self.assertEqual(errors,[])
                    self.assertEqual([e["kind"] for e in self.notices()],
                                     ["assistant_group_created","dispatch_sent","dispatch_finished"])
        finally:
            gate.set()
            for server in (main,preview):
                server.shutdown()
                server.server_close()
