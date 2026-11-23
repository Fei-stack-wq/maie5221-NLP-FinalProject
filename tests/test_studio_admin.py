import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from miniagent import studio_admin, studio_artifacts, studio_cleanup
from miniagent.evaluation import snapshot
from miniagent.folders import sync
from miniagent.studio import Studio, StudioError
from miniagent.studio_tools import assistant_tools
from miniagent.workbench import published_bytes
from tests.helpers import FakeProvider, Script, call, reply, settings, tool_results


class StudioAdminTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.script = Script()
        self.fake = FakeProvider(self.script)
        self.studio = Studio(settings(self.root/"runs"), lambda _:self.fake)
        self.addCleanup(self.studio.close)
        self.assistant = self.studio.assistant()["id"]
        self.gid = self.studio.create_group("软件组",[{"id":"dev","name":"开发","prompt":"交付"}],"dev")["id"]
        self.sid = self.studio.create_session(self.gid,"看板")["id"]
        self.room = self.studio.group(self.sid)

    def manage(self, op, gid=None, **options):
        return studio_admin.manage(self.studio,op,gid or self.sid,**options)

    def ask(self, text, *responses):
        self.script.queues["assistant"] = list(responses)
        return self.studio.post(self.assistant, text)

    def card(self):
        events = self.studio.group(self.assistant).chat()
        return next((e for e in reversed(events) if e["kind"] == "approval_requested"), None)

    def approve(self):
        return self.studio.decide_approval(self.assistant, self.card()["approval_id"], "approve")

    def post_action(self, text, op, group_id, **kw):
        """Ask the assistant for one data operation, then confirm its card as the user would."""
        self.ask(text, reply("", call("delete_studio_data", operation=op, group_id=group_id, **kw)),
                 reply("请确认。"))
        return self.approve()

    def publish(self, room, number, files):
        tid = "t-" + format(number,"012x")
        working = room.workspace("dev")
        baseline = room.project.checkout(working)
        for name, value in files.items():
            (working/name).write_text(value)
        changes,_ = room.project.publish(working,baseline,author="dev",task_id=tid)
        room.save_task({"id":tid,"status":"completed","primary":"dev","text":"交付","created_at":number,
                        "trigger_seq":1,"final_answer":"完成"})
        room.append("report",task_id=tid,agent="dev",changes=[c.as_dict() for c in changes])
        snapshot(room,tid)
        return tid

    def test_chat_query_can_inspect_another_team_without_commissioning_work(self):
        self.room.append("user_message",text="灯塔编号 405")
        def answer(messages):
            self.assertIn("灯塔编号 405",messages[-1]["content"])
            return reply("看板会话里的编号是 405。")
        res = self.ask("看板会话里说的编号是什么？",reply("",call("inspect_studio",view="chat",group_id=self.sid)),answer)
        self.assertIn("405",res["text"])
        self.assertEqual(self.studio.tasks(self.assistant),[])
        self.assertIsNone(self.card())
        self.script.queues["room"]=[reply("本群回复。")]
        self.studio.post(self.gid,"我们在聊什么？")
        self.assertNotIn("inspect_studio",self.fake.for_agent("room")[-1]["tools"])
        self.assertNotIn("delete_studio_data",self.fake.for_agent("room")[-1]["tools"])
        tools = set(self.fake.for_agent("assistant")[-1]["tools"])
        self.assertTrue({"inspect_studio","delete_studio_data","delete_role","set_studio_models","redispatch_task"} <= tools)
    def test_deletes_current_and_all_snapshots_but_keeps_locally_modified_copy(self):
        external = self.root/"bound"
        external.mkdir()
        (external/"notes.txt").write_text("my file")
        self.studio.update_group(self.sid,{"folder":str(external)})
        sync(self.room,importing=True)
        a = self.publish(self.room,1,{"dashboard.html":"one","report.md":"old","keep.txt":"retain"})
        sync(self.room,importing=False)
        b = self.publish(self.room,2,{"dashboard.html":"two","report.md":"new"})
        sync(self.room,importing=False)
        (external/"report.md").write_text("my edits")
        source=self.studio.group(self.assistant)
        source.append("dispatch_finished",target_group_id=self.sid,target_task_id=b,group_name="看板",status="completed",artifacts=[{"path":"dashboard.html"},{"path":"keep.txt"}])
        self.assertEqual(studio_admin.inspect(self.studio,"file",self.sid,path="dashboard.html",task_id=a)["text"],"one")
        res=self.post_action("删看板的两个产物","delete_artifacts",self.sid,paths=["dashboard.html","report.md"])
        self.assertIn("2 个产物",res["text"])
        self.assertIn("保留：report.md",res["text"])
        self.assertFalse((external/"dashboard.html").exists())
        self.assertEqual((external/"report.md").read_text(),"my edits")
        self.assertEqual((external/"notes.txt").read_text(),"my file")
        for tid in (a,b):
            self.assertIsNone(published_bytes(self.room,tid,"report.md"))
            with zipfile.ZipFile(self.room.task_dir(tid)/"artifacts.zip") as z:
                self.assertNotIn("dashboard.html",z.namelist())
                if tid == a:
                    self.assertEqual(z.read("keep.txt"),b"retain")
        for e in self.room.chat():
            self.assertFalse({"dashboard.html","report.md"} & {c["path"] for c in e.get("changes",[])})
        self.assertEqual(next(e for e in source.chat() if e["kind"]=="dispatch_finished")["artifacts"],[{"path":"keep.txt"}])
        self.assertFalse((self.room.workspace("dev")/"dashboard.html").exists())
        self.assertEqual({r["path"] for r in self.room.project.files()},{"notes.txt","keep.txt"})
        self.assertEqual(studio_admin.inspect(self.studio,"artifacts",self.sid)["total"],1)
        for args in ({"paths":["../escape"]},{"paths":["notes.txt"]},{"paths":[]},{"paths":["keep.txt"],"all_files":True}):
            with self.assertRaises(ValueError):
                self.manage("delete_artifacts",**args)
        self.assertTrue((self.room.project.root/"keep.txt").is_file())

    def test_deletion_recovery_and_shared_folder(self):
        ext = self.root/"shared"
        ext.mkdir()
        self.studio.update_group(self.sid,{"folder":str(ext)})
        self.publish(self.room,1,{"shared.md":"same"})
        sync(self.room,importing=False)
        second = self.studio.create_group("另一个群",[{"id":"dev","name":"开发","prompt":"交付"}],"dev")["id"]
        other = self.studio.group(second)
        self.studio.update_group(second,{"folder":str(ext)})
        sync(other,importing=True)
        with patch.object(studio_artifacts,"strip_snapshot",side_effect=OSError("disk temporarily unavailable")):
            with self.assertRaises(OSError):
                self.manage("delete_artifacts",all_files=True)
        self.assertTrue((self.room.root/".pending-artifact-delete.json").is_file())
        again=Studio(self.studio.settings)
        self.addCleanup(again.close)
        again.restore_queue()
        self.assertFalse((self.room.root/".pending-artifact-delete.json").exists())
        self.assertEqual((ext/"shared.md").read_text(),"same")
        self.assertTrue((other.project.root/"shared.md").is_file())
        self.assertEqual(studio_admin.inspect(again,"artifacts",self.sid)["total"],0)

    def test_clear_chat_and_restart_without_resetting_cursor_tasks_or_other_sessions(self):
        sibling=self.studio.group(self.gid)
        sibling.append("user_message",text="private to default")
        self.room.append_notice("system","old-notice",text="old news")
        self.room.append("user_message",text="erase this",client_id="msg1")
        tid=self.publish(self.room,1,{"deliver.md":"safe"})
        last=self.room.chat()[-1]["seq"]
        self.assertIn("已清空",self.post_action("把看板聊天清空","clear_chat",self.sid)["text"])
        self.assertEqual([e["kind"] for e in self.room.chat()],["chat_cleared"])
        self.assertIsNone(self.room.client_message("msg1"))
        again=Studio(self.studio.settings)
        self.addCleanup(again.close)
        fresh=again.group(self.sid)
        fresh.append_notice("system","old-notice",text="should not reappear")
        self.assertEqual(len(fresh.chat()),1)
        self.assertGreater(fresh.append("user_message",text="new")["seq"],last)
        self.assertEqual(published_bytes(fresh,tid,"deliver.md"),b"safe")
        self.assertIn("private to default",str(sibling.chat()))
        self.assertNotIn("erase this",(fresh.root/"chat.jsonl").read_text())

    def test_delete_one_session_then_reset_default_and_delete_whole_group(self):
        sibling=self.studio.create_session(self.gid,"保留")["id"]
        sibling_room=self.studio.group(sibling)
        self.publish(sibling_room,2,{"keep.md":"keep"})
        self.publish(self.room,1,{"remove.md":"remove"})
        self.studio.roles.propose("dev","only here","group:"+self.sid)
        self.studio.roles.propose("dev","shared knowledge","general")
        self.assertEqual(self.manage("delete_session")["deleted_ids"],[self.sid])
        self.assertFalse(self.room.root.exists())
        self.assertFalse(self.studio.store.trash_path(self.sid).exists())
        self.assertEqual([x["text"] for x in self.studio.roles.entries("dev")],["shared knowledge"])
        with self.assertRaises(StudioError):
            self.studio.group(self.sid)
        self.publish(self.studio.group(self.gid),3,{"default.txt":"old"})
        self.assertEqual(self.manage("delete_session",self.gid)["reset_default"],self.gid)
        self.assertEqual(self.studio.tasks(self.gid),[])
        self.assertEqual((sibling_room.project.root/"keep.md").read_text(),"keep")
        self.assertEqual(self.studio.group(self.gid).group.lead,"dev")
        with self.assertRaises(ValueError):
            self.manage("delete_group",sibling)
        self.studio.trash(sibling)
        self.assertEqual(set(self.manage("delete_group",self.gid)["deleted_ids"]),{self.gid,sibling})
        self.assertEqual(studio_admin.inspect(self.studio,"trash")["rows"],[])

    def test_trash_restore_and_permanent_removal_and_busy_scope(self):
        self.room.append("user_message",text="survives trash")
        with self.room.turn_lock:
            with self.assertRaisesRegex(ValueError,"未结束"):
                self.manage("delete_session")
        self.studio.trash(self.sid)
        self.assertEqual(studio_admin.inspect(self.studio,"trash")["rows"][0]["id"],self.sid)
        self.manage("restore_trash")
        self.assertIn("survives trash",str(self.studio.group(self.sid).chat()))
        self.studio.trash(self.sid)
        self.manage("purge_trash")
        self.assertFalse(self.studio.store.trash_path(self.sid).exists())
        with self.assertRaises(ValueError):
            self.manage("restore_trash","../escape")

    def test_open_browser_observes_remote_clear_then_permanent_delete(self):
        from playwright.sync_api import expect, sync_playwright
        from miniagent.server import serve
        secret="本条测试记录应在清空后消失"
        self.room.append("user_message",text=secret, addressed="dev")
        self.script.queues["assistant"]=[
            reply("",call("delete_studio_data",operation="clear_chat",group_id=self.sid)),reply("已列入确认卡片。"),
            reply("",call("delete_studio_data",operation="delete_session",group_id=self.sid)),reply("已列入确认卡片。"),
            reply("",call("delete_role",role="dev"),call("delete_studio_data",operation="delete_group",group_id=self.gid)),
            reply("已列出清单，等你确认。")]
        main,preview=serve(self.studio,0)
        try:
            with sync_playwright() as pw:
                chrome=Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
                with pw.chromium.launch(**({"executable_path":str(chrome)} if chrome.exists() else {})) as browser:
                    base=f"http://127.0.0.1:{main.server_port}/#"
                    assistant=browser.new_page()
                    assistant.goto(base+self.assistant)
                    target=browser.new_page()
                    target.goto(base+self.sid)
                    expect(target.locator("#timeline")).to_contain_text(secret)
                    for text,receipt in [("清空看板的聊天","已清空"),("彻底删除看板会话","已彻底删除")]:
                        assistant.locator("#input").fill(text)
                        assistant.locator("#composer button[type=submit]").click()
                        card=assistant.locator(".msg.approval").last
                        expect(card).to_contain_text("看板")
                        card.get_by_role("button",name="确认执行").click()
                        expect(assistant.locator("#timeline")).to_contain_text(receipt)
                        if text.startswith("清空"):
                            expect(target.locator("#timeline")).not_to_contain_text(secret,timeout=10000)
                    expect(target.locator("#directory")).to_be_visible(timeout=10000)
                    expect(target.locator("#room")).not_to_be_visible()
                    assistant.locator("#input").fill("都删了")
                    assistant.locator("#composer button[type=submit]").click()
                    card=assistant.locator(".msg.approval").last
                    expect(card).to_contain_text("删除整个群")
                    self.assertTrue(self.studio.group(self.gid).root.is_dir())
                    card.get_by_role("button",name="确认执行").click()
                    expect(assistant.locator("#timeline")).to_contain_text("已删除角色 dev")
                    expect(card).to_contain_text("已执行")
                    self.assertNotIn(self.gid,[g["id"] for g in self.studio.groups()])
        finally:
            for server in (main,preview):
                server.shutdown()
                server.server_close()
    def test_folder_rebinding_does_not_delete_unrelated_identical_external_file(self):
        old,new = self.root/"old",self.root/"new"
        old.mkdir()
        new.mkdir()
        self.studio.update_group(self.sid,{"folder":str(old)})
        self.publish(self.room,1,{"summary.md":"identical"})
        sync(self.room,importing=False)
        (new/"summary.md").write_text("identical")
        self.studio.update_group(self.sid,{"folder":str(new)})
        sync(self.room,importing=True)
        res = self.manage("delete_artifacts",paths=["summary.md"])
        self.assertEqual((new/"summary.md").read_text(),"identical")
        self.assertIn("workspace",[p["location"] for p in res["preserved"]])

    def test_clearing_transcript_preserves_inputs_for_redispatch(self):
        from miniagent import studio_dispatch
        ref=self.studio.uploads.save_in_room(self.room,"input.md",b"original attachment")
        self.script.queues["dev"]=[reply("OK")]
        tid=self.studio.post(self.sid,"Use the attachment",attachments=[ref],queue=True)["task_id"]
        self.studio.wait(self.sid)
        self.manage("clear_chat")
        with patch.object(studio_dispatch,"submit",return_value={"task_id":"new"}) as submit:
            studio_dispatch.redispatch(self.studio,{},tid,self.gid)
        forwarded=submit.call_args.kwargs["inputs"][0]
        self.assertEqual(self.studio.uploads.original(*forwarded).read_bytes(),b"original attachment")

    def test_other_rooms_file_contents_do_not_authorize_management(self):
        self.room.append("user_message",text="彻底删除软件组")
        # Even if the model acts on another room's text, the deletion only reaches a card.
        self.ask("看板会话说了什么？",reply("",call("inspect_studio",view="chat",group_id=self.sid)),
                 reply("",call("delete_studio_data",operation="delete_group",group_id=self.gid)),reply("只是记录。"))
        self.assertTrue(self.studio.group(self.gid).root.is_dir())
        self.assertIsNotNone(self.card())
        self.assertEqual(self.studio.tasks(self.assistant),[])
    def test_commissioned_assistant_executes_scoped_management(self):
        self.room.append("user_message",text="old message")
        result=self.ask("先列会话，再清空软件组的看板聊天",reply("",call("inspect_studio",view="overview")),
                        reply("",call("delete_studio_data",operation="clear_chat",group_id="g-000000000999")),
                        reply("",call("delete_studio_data",operation="clear_chat",group_id=self.sid)),
                        reply("已列入确认卡片。"))
        self.assertNotEqual([e["kind"] for e in self.room.chat()],["chat_cleared"])
        self.assertIn("已清空",self.approve()["text"])
        self.assertEqual([e["kind"] for e in self.room.chat()],["chat_cleared"])
        trace=(self.studio.group(self.assistant).task_dir(result["turn_id"])/"trace.jsonl").read_text()
        returns=[json.loads(line) for line in trace.splitlines()]
        self.assertEqual([r["ok"] for r in returns if r.get("type")=="tool_result" and r.get("tool")=="delete_studio_data"],[False,True])
    def test_short_cleanup_reply_resolves_three_teams_and_only_their_roles(self):
        def group(name, *roles):
            return self.studio.create_group(name,[{"id":r,"name":r,"prompt":"协作"} for r in roles],roles[0])["id"]
        selected = [group(name, *(prefix+str(i) for i in range(4)), "shared")
                    for name,prefix in [("测试软件组","s1r"),("编辑部","s2r"),("基金组","s3r")]]
        extra = group("项目交付组","delivery","shared")
        archived = group("归档测试组","observer")
        self.studio.update_group(archived,{"archived":True})
        external = self.root/"published"
        external.mkdir()
        child = self.studio.create_session(selected[0],"仪表盘")["id"]
        self.studio.update_group(child,{"folder":str(external)})
        room = self.studio.group(child)
        self.publish(room,1,{"memo.md":"published","edit.txt":"original"})
        sync(room,importing=False)
        (external/"edit.txt").write_text("local edit")
        assistant = self.studio.group(self.assistant)
        assistant.append("room_reply",text="三个场景群可清理，另有项目交付组和归档测试组。还要清理绑定副本吗？",agent="assistant")
        # Some providers emit a separate call per team rather than a single call with three IDs.
        self.ask("都删了",reply("",call("inspect_studio",view="overview"),
                 *(call("delete_groups",group_ids=[g],include_roles=True,include_artifacts=True) for g in selected)),
                 reply("已列出三个群，等你确认。"))
        items = " ".join(self.card()["items"])
        for gid in selected:
            self.assertIn(gid,items)
            self.assertTrue(self.studio.group(gid).root.is_dir())
        self.assertNotIn(extra,items)
        self.assertNotIn(archived,items)
        self.assertEqual(self.card()["roles_kept"],["shared"])
        self.assertIn("未修改的绑定副本",items)
        self.assertTrue((external/"memo.md").is_file())
        approval = self.card()["approval_id"]
        # Confirm through a fresh Studio using only the saved room record.
        self.studio.close()
        self.studio = Studio(settings(self.root/"runs"),lambda _:self.fake)
        self.addCleanup(self.studio.close)
        result = self.studio.decide_approval(self.assistant, approval, "approve")
        assistant = self.studio.group(self.assistant)
        receipt = assistant.chat()[-1]
        self.assertEqual(result["decision"],"approved")
        self.assertTrue(all(item["ok"] for item in receipt["results"]))
        self.assertEqual(receipt["not_run"],[])
        self.assertIn("保留角色：shared",receipt["text"])
        self.assertEqual(sum(row["step"].startswith("删除角色") for row in receipt["results"]),12)
        self.assertFalse((external/"memo.md").exists())
        self.assertEqual((external/"edit.txt").read_text(),"local edit")
        self.assertEqual(self.studio.tasks(self.assistant),[])
        self.assertEqual({r["id"] for r in self.studio.groups()},{self.assistant,extra,archived,self.gid})
        self.assertTrue({"shared","delivery","observer","dev"} <= {r["id"] for r in self.studio.roles.list()})
    def test_card_validates_targets_then_orders_and_deduplicates(self):
        remove_group = call("delete_studio_data",operation="delete_group",group_id=self.gid)
        remove_role = call("delete_role",role="dev")
        def refused(messages):
            self.assertIn("Error",tool_results(messages)[-1])
            return reply("",remove_role,remove_group,remove_group)
        self.ask("删除软件组以及开发角色",
                 reply("",call("delete_studio_data",operation="delete_group",group_id="g-000000000999")),
                 refused,reply("请确认。"))
        with self.room.turn_lock:
            self.assertIn("未结束",self.approve()["text"])
        self.assertTrue(self.studio.group(self.gid).root.is_dir())
        self.ask("再列一次",reply("",remove_role,remove_group),reply("请确认。"))
        card=self.card()
        self.assertEqual(len(card["items"]),2)
        self.assertTrue(card["items"][0].startswith("删除整个群"))
        self.assertIn("已删除角色 dev",self.approve()["text"])
        self.assertEqual(self.studio.tasks(self.assistant),[])
    def test_cleanup_card_expires_when_scope_changes_and_can_be_declined_in_chat(self):
        propose=lambda: reply("",call("delete_groups",group_ids=[self.gid],include_roles=True,include_artifacts=False))
        self.ask("都删了",propose(),reply("请确认。"))
        first=self.card()
        self.studio.create_session(self.gid,"新的工作")
        self.assertEqual(self.approve()["decision"],"expired")
        self.assertTrue(self.studio.group(self.gid).root.is_dir())
        self.ask("再列一次",propose(),reply("请确认。"))
        second=self.card()
        self.assertNotEqual(first["approval_id"],second["approval_id"])
        self.assertIn("3 个会话"," ".join(second["items"]))
        self.ask("先保留",reply("",call("answer_confirmation",approval_id=second["approval_id"],decision="reject")),
                 reply("好的，先保留。"))
        self.assertTrue(self.studio.group(self.gid).root.is_dir())
        self.ask("确认",reply("没有待确认的清单。"))
        self.assertNotIn("answer_confirmation",self.fake.for_agent("assistant")[-1]["tools"])

    def test_mid_batch_error_receipt_keeps_remaining_team_and_role(self):
        self.ask("删除软件组和专属角色，清理绑定副本",
                 reply("",call("delete_groups",group_ids=[self.gid],include_roles=True,include_artifacts=True)),
                 reply("请确认。"))
        original = studio_admin.manage
        def fail_on_group(*args, **kwargs):
            if kwargs["operation"]=="delete_group":
                raise OSError("disk unavailable")
            return original(*args,**kwargs)
        with patch.object(studio_admin,"manage",side_effect=fail_on_group):
            result=self.approve()
        self.assertIn("disk unavailable",result["text"])
        receipt=self.studio.group(self.assistant).chat()[-1]
        self.assertEqual(result["decision"],"partial")
        self.assertEqual([x["ok"] for x in receipt["results"]],[True,True,False])
        self.assertEqual(len(receipt["not_run"]),1)
        self.assertTrue(self.studio.group(self.gid).root.is_dir())
        self.assertEqual(self.studio.roles.get("dev")["name"],"开发")
    def test_deletion_mixed_with_other_steps_still_waits_for_the_card(self):
        self.ask("处理软件组",reply("",call("delete_studio_data",operation="delete_group",group_id=self.gid),
                                    call("update_group_settings",group_id=self.sid,name="看板二期")),reply("请确认删除。"))
        self.assertTrue(self.studio.group(self.gid).root.is_dir())
        self.assertEqual(self.studio.group(self.sid).group.name,"看板二期")
        self.assertIsNotNone(self.card())
        self.assertEqual(self.studio.tasks(self.assistant),[])
    def test_dm_and_its_exclusive_role_can_be_removed_together(self):
        dm=self.studio.create_group("私人助理",[{"id":"helper","name":"助手甲","prompt":"协助"}],"helper",kind="dm")["id"]
        self.ask("删除私人助理会话以及助手甲这个角色",
                 reply("",call("delete_role",role="helper"),call("delete_studio_data",operation="delete_session",group_id=dm)),
                 reply("请确认。"))
        self.assertIn("已删除角色 helper",self.approve()["text"])
        self.assertNotIn(dm,[g["id"] for g in self.studio.groups()])
    def test_duplicate_team_names_need_a_concrete_identity(self):
        other = self.studio.create_group("软件组",[{"id":"dev","name":"开发","prompt":"交付"}],"dev")["id"]
        self.ask("删除软件组",reply("",call("delete_studio_data",operation="delete_group",group_id=self.gid)),
                 reply("有两个软件组，卡片上是 "+self.gid+"。"))
        self.assertIn(self.gid," ".join(self.card()["items"]))
        self.assertNotIn(other," ".join(self.card()["items"]))
        self.assertTrue(all(self.studio.group(g).root.is_dir() for g in (self.gid,other)))