"""One team roster, independently addressable conversations and published workspaces."""
import io
import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from miniagent.server import serve
from miniagent.studio import Studio, StudioError
from tests.helpers import FakeProvider, Script, call, reply, settings
from tests.test_team import TEAM, finished


class GroupSessionTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.provider = FakeProvider(Script())
        self.studio = Studio(settings(self.root / "runs"), lambda _: self.provider)
        self.addCleanup(self.studio.close)
        self.team = self.studio.create_group("产品团队", TEAM, "lead")["id"]
        self.owner = self.studio.group(self.team)

    def test_default_keeps_legacy_bytes_and_children_share_only_team_settings(self):
        (self.owner.project.root / "legacy.md").write_text("Original")
        self.owner.append("user_message",text="legacy thread")
        original = (self.owner.root / "chat.jsonl").read_bytes()
        sid = self.studio.create_session(self.team,"独立项目")["id"]
        child = self.studio.group(sid)
        self.assertEqual((self.owner.root / "chat.jsonl").read_bytes(), original)
        self.assertEqual(child.project.files(), [])
        self.assertFalse((child.root / "group.json").exists())
        self.assertNotIn("members", json.loads((child.root / "session.json").read_bytes()))
        self.studio.change_members(sid, remove_member_ids=["lead"], lead="qa")
        for room in self.studio.sessions(self.team):
            self.assertEqual(room["lead"], "qa")
            self.assertEqual([m["id"] for m in room["members"]], ["dev","qa"])
        folder = self.root / "outside"
        nested = folder / "sub"; nested.mkdir(parents=True)
        self.studio.update_group(sid, {"folder":str(nested)})
        for conflict in (folder,nested):
            with self.assertRaisesRegex(StudioError,"重叠"):
                self.studio.update_group(self.team, {"folder":str(conflict)})
        self.assertEqual(self.owner.group.folder,"")
        self.assertEqual(child.group.folder,str(nested.resolve()))
        ref = self.studio.uploads.save_in_room(child,"brief.txt",b"CHILD ATTACHMENT")
        self.assertEqual(self.studio.uploads.original(child,ref).read_bytes(),b"CHILD ATTACHMENT")
        with self.assertRaisesRegex(ValueError,"another conversation"):
            self.studio.post(self.team,"oops",attachments=[ref])
        with self.assertRaises(ValueError):
            self.studio.post(self.team,"oops",attachments=[{"id":ref["id"]}])
        self.owner.remember("dev","private default note")
        self.assertEqual(child.memory("dev"),"")
        service = Studio(self.studio.settings,lambda _:self.provider)
        self.addCleanup(service.close)
        self.assertEqual(service.group(sid).group.lead,"qa")
        self.assertEqual(service.group(sid).group.folder,str(nested.resolve()))
        self.assertEqual(service.read_file(self.team,"legacy.md").read_text(),"Original")
        self.assertEqual([r["id"] for r in service.sessions(self.team)], [self.team,sid])
        self.assertEqual([r["id"] for r in service.groups()],[self.team])
        service.trash(self.team)
        for room in (self.team,sid):
            with self.assertRaises(StudioError):
                service.group(room)

    def test_parallel_sessions_have_distinct_workers_and_queues(self):
        sid = self.studio.create_session(self.team,"网站")["id"]
        gate, started = threading.Event(), threading.Event()
        def respond(agent,messages,tools,options):
            if agent=="lead" and "WAIT-IN-A" in messages[-1]["content"]:
                started.set()
                if not gate.wait(15): raise AssertionError("A did not resume")
            return reply("done")
        self.provider.respond=respond
        try:
            first=self.studio.post(self.team,"WAIT-IN-A",queue=True)["task_id"]
            self.assertTrue(started.wait(5))
            nxt=self.studio.post(self.team,"same room next task",queue=True)
            self.assertEqual(nxt["route"],"queued")
            other=self.studio.post(sid,"@dev B independent",queue=True)
            self.assertEqual(other["route"],"task")
            self.assertEqual(finished(self.studio,sid)["status"],"completed")
            self.assertEqual(self.studio.state(self.team)["live"]["task_id"],first)
            self.assertIsNone(self.studio.state(sid)["live"])
            with self.assertRaises(StudioError): self.studio.trash(self.team)
        finally:
            gate.set()
        self.studio.wait(self.team)
        finished(self.studio,self.team)
        self.assertEqual([r["status"] for r in self.studio.tasks(self.team)],["completed","completed"])

    def test_browser_switch_and_mentions_preserve_context_files_and_attachments(self):
        room_inputs=[]
        def route(messages):
            context=json.loads(messages[1]["content"].split("\n",1)[1])
            room_inputs.append(context)
            request=messages[-1]["content"]
            if "交付" in request:
                return reply("",call("start_room_task",quote="交付"))
            return reply("已核对本会话")
        def write(text):
            return reply("",call("write_file",path="result.md",content="# "+text))
        self.provider.respond=Script(room=[route,route,route,route],
            lead=[reply("",call("delegate_task",assignee="dev",task="根据本次任务的附件编写 result.md，记录项目标识。",deliverables=["result.md"])),reply("默认已交付")],
            dev=[write("A-ONLY"),reply("A-ONLY 完成")],
            qa=[write("B-ONLY"),reply("B-ONLY 完成"),reply("",call("read_file",path="result.md")),reply("复用 B-ONLY 完成")])
        main,preview=serve(self.studio,0)
        self.addCleanup(lambda: [(s.shutdown(),s.server_close()) for s in (main,preview)])
        base=f"http://127.0.0.1:{main.server_port}/"
        chrome=Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        with sync_playwright() as pw, pw.chromium.launch(**({"executable_path":str(chrome)} if chrome.exists() else {})) as browser:
            page=browser.new_page()
            errors=[];page.on("pageerror",lambda err:errors.append(str(err)))
            page.goto(base+"#"+self.team)
            box=page.locator("#input")
            def attach(name,content):
                page.locator("#uploads").set_input_files({"name":name,"mimeType":"text/plain","buffer":content})
            def submit(text):
                box.fill(text);box.press("Enter")
            def read_result(mark):
                page.locator("#toggle-files").click()
                page.locator('[data-path="result.md"]').click()
                expect(page.locator("#reader-body h1")).to_have_text(mark)
                page.locator("#reader-close").click();page.locator("#close-files").click()
            expect(page.locator("#room-session option:checked")).to_have_text("默认会话")
            attach("alpha.txt",b"A-ONLY")
            submit("交付 A-ONLY")
            expect(page.locator("#timeline .msg.final")).to_contain_text("默认已交付")
            read_result("A-ONLY")
            box.fill("未发的 A 草稿");attach("unsent.txt",b"draft")
            page.get_by_role("button",name="新建会话",exact=True).click()
            page.get_by_label("会话名称").fill("官网")
            page.get_by_role("button",name="创建会话",exact=True).click()
            expect(page.locator("#room-session option:checked")).to_have_text("官网")
            sid=page.url.split("#")[1]
            expect(box).to_have_value("")
            expect(page.locator("#attachment-chips")).not_to_contain_text("unsent")
            expect(page.locator("#timeline")).not_to_contain_text("A-ONLY")
            page.locator("#toggle-files").click()
            expect(page.locator("#file-list")).to_contain_text("还没有文件")
            page.locator("#close-files").click()
            box.fill("@");expect(page.get_by_role("option",name="Tester @qa")).to_be_visible()
            box.press("Escape");expect(page.locator("#mention-list")).to_be_hidden()
            box.fill("请 @");page.get_by_role("option",name="Tester @qa").click()
            expect(box).to_have_value("请 @qa ")
            box.fill("@Tes");box.press("Enter")
            expect(box).to_have_value("@qa ")
            self.assertEqual(self.studio.tasks(sid),[])
            box.press("End");box.press_sequentially("B");attach("beta.txt",b"B-ONLY")
            submit("@qa 交付 B-ONLY")
            expect(page.locator("#timeline .msg.final")).to_contain_text("B-ONLY 完成")
            read_result("B-ONLY")
            submit("@qa 继续这项交付")
            expect(page.locator("#timeline .msg.final").last).to_contain_text("复用 B-ONLY 完成")
            submit("完成了吗")
            expect(page.locator("#timeline")).to_contain_text("已核对本会话")
            self.assertEqual(len(self.studio.tasks(sid)),2)
            self.assertEqual(len(self.studio.tasks(self.team)),1)
            self.assertEqual(self.studio.tasks(sid)[0]["primary"],"qa")
            self.assertNotIn("A-ONLY",json.dumps(room_inputs[1:],ensure_ascii=False))
            self.assertNotIn("alpha.txt",json.dumps(room_inputs[1:],ensure_ascii=False))
            self.assertEqual([a["name"] for a in room_inputs[-1]["attachments"]],["beta.txt"])
            self.assertIn("B-ONLY",json.dumps(room_inputs[-1],ensure_ascii=False))
            for req in self.provider.for_agent("qa"):
                self.assertNotIn("A-ONLY",json.dumps(req["messages"],ensure_ascii=False))
            self.assertIn("B-ONLY",self.provider.for_agent("qa")[-1]["messages"][-1]["content"])
            for rid,expected in ((self.team,"A-ONLY"),(sid,"B-ONLY")):
                with zipfile.ZipFile(io.BytesIO(self.studio.archive(rid))) as ar:
                    self.assertEqual(ar.read("result.md").decode(),"# "+expected)
            page.locator("#room-session").select_option(self.team)
            expect(box).to_have_value("未发的 A 草稿")
            expect(page.locator("#attachment-chips")).to_contain_text("unsent.txt")
            expect(page.locator("#timeline .msg.final")).to_contain_text("默认已交付")
            expect(page.locator("#timeline")).not_to_contain_text("B-ONLY")
            read_result("A-ONLY")
            page.goto(base+"#"+sid+"/task/"+self.studio.tasks(sid)[0]["id"])
            expect(page.locator("#sheet")).to_be_visible()
            self.assertEqual(errors,[])
