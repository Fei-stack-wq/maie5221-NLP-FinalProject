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

    def message(self, text, response):
        self.script.queues["room"] = [response]
        return self.studio.post(self.aid, text)

    def dispatched(self, target, text):
        self.script.queues["assistant"] = [reply("",call("start_task",group_id=target,text=text)),
                                           reply("Dispatched to the requested conversation.")]
        result = self.message(text, action("start_room_task",text))
        self.studio.wait(self.aid)
        return result["task_id"], self.studio.tasks(target)[-1]["id"]

    def test_read_only_followup_and_assistant_does_commission_itself(self):
        store = self.studio.group(self.aid)
        before = file_hashes(store.root)
        def discuss(messages):
            ctx = evidence(messages)
            self.assertEqual(ctx["kind"], "assistant")
            self.assertEqual({r["id"] for r in ctx["directory"]["roles"]}, {"editor","analyst","reviewer"})
            self.assertEqual(ctx["dispatches"], [])
            return reply("Editor can handle that.")
        self.assertEqual(self.message("@editor 适合做什么？", discuss)["route"], "reply")
        self.assertEqual(self.studio.tasks(self.aid), [])
        after = file_hashes(store.root)
        self.assertEqual({p for p in after if after[p]!=before.get(p)}, {"chat.jsonl"})
        self.script.queues["assistant"] = [reply("",call("write_file",path="memo.md",content="# Memo")),reply("Wrote memo.md.")]
        text = "你直接写一份备忘。"
        tid = self.message(text, action("start_room_task",text))["task_id"]
        self.studio.wait(self.aid)
        self.assertEqual(self.studio.tasks(self.team), [])
        worker = self.provider.for_agent("assistant")[0]
        self.assertIn("Desk",worker["messages"][1]["content"])
        self.assertTrue({"start_task","create_group","change_group_members","write_file"} <= set(worker["tools"]))
        before = file_hashes(store.root)
        def recap(messages):
            task = evidence(messages)["tasks"][-1]
            self.assertEqual(task["id"],tid)
            self.assertEqual(task["file_changes"][0]["path"],"memo.md")
            return reply("我写好了 memo.md。")
        self.assertEqual(self.message("你都做了啥",recap)["text"], "我写好了 memo.md。")
        self.assertEqual(len(self.studio.tasks(self.aid)),1)
        after = file_hashes(store.root)
        self.assertEqual({p for p in after if after[p]!=before.get(p)}, {"chat.jsonl"})

    def test_membership_from_chat_is_atomic_visible_and_idempotent(self):
        store = self.studio.group(self.team)
        def change(text, **args):
            return self.message(text,reply("",call("change_group_members",group_id=self.team,quote=text,**args)))
        def roster():
            return [(m.id, m.note) for m in store.group.members]
        self.studio.update_group(self.team,{"members":[{**store.group.members[0].__dict__,"note":"Desk style"}]})
        change("把 Reviewer 拉进 Desk。",add_role_ids=["reviewer"])
        self.assertEqual(roster(),[("editor","Desk style"),("reviewer","")])
        updates = len(store.chat())
        change("把 Reviewer 加进群。",add_role_ids=["reviewer"])
        self.assertEqual(len(store.chat()), updates)
        rejected = change("把 Analyst 和 Missing 加进来。",add_role_ids=["analyst","missing"])
        self.assertIn("未完成",rejected["text"])
        self.assertEqual(len(store.chat()),updates)
        self.assertIn("未完成",change("移除 Editor。",remove_member_ids=["editor"])["text"])
        result = change("让 Reviewer 接手，移出 Editor。",lead="reviewer",remove_member_ids=["editor"])
        self.assertIn("@reviewer",result["text"])
        self.assertEqual((roster(),store.group.role_ids),([("reviewer","")],["reviewer"]))
        self.assertEqual([m["id"] for m in store.chat()[-1]["members"]], ["reviewer"])
        self.assertEqual(self.studio.roles.get("editor")["name"],"Editor")
        self.assertEqual(self.studio.tasks(self.aid),[])
        self.assertEqual(self.studio.tasks(self.team),[])
        before = file_hashes(store.root)
        self.message("只是问问群里有谁",reply("",call("change_group_members",group_id=self.team,quote="拉人",add_role_ids=["editor"])))
        self.message("目录变化",reply("",call("change_group_members",group_id=self.team,quote="目录变化",add_role_ids=[{}])))
        self.assertEqual(file_hashes(store.root),before)
        for gid in (self.dm,self.aid):
            with self.assertRaises(StudioError):
                self.studio.change_members(gid,add_role_ids=["editor"])

    def test_progress_and_steering_after_parent_finished_only_reaches_dispatched_child(self):
        self.script.queues["analyst"] = [reply("",call("ask_human",question="Which title?")),
                                         reply("",call("write_file",path="plan.md",content="Short and blue")), reply("Wrote plan.md.")]
        parent, child = self.dispatched(self.dm,"给 Analyst 一项写作任务。")
        target = self.studio.group(self.dm)
        question = wait_for(lambda:(self.studio.state(self.dm)["live"] or {}).get("question"))
        self.assertEqual(target.load_task(child)["dispatched_by"],{"group_id":self.aid,"task_id":parent})
        self.assertIsNone(self.studio.state(self.aid)["live"])
        def status(messages):
            ctx = evidence(messages)
            self.assertEqual(ctx["action_targets"][child]["question"]["id"],question["id"])
            self.assertEqual(ctx["dispatches"][0]["group_name"],"Private")
            self.assertIsNone(ctx["active_task"])
            return reply("Analyst is waiting for a title.")
        before = file_hashes(target.root)
        self.assertEqual(self.message("那边卡在哪？",status)["route"],"reply")
        self.assertEqual(file_hashes(target.root),before)
        self.message("无关的目标",reply("",call("change_current_task",quote="无关的目标",task_id=parent)))
        self.assertEqual(file_hashes(target.root),before)
        change, answer = "控制在一页内。", "标题用 Blue。"
        self.assertEqual(self.message(change,action("change_current_task",change)),{"route":"steering","task_id":child})
        self.assertEqual(self.studio.state(self.dm)["live"]["question"],question)
        self.assertEqual(self.message(answer,action("answer_open_question",answer)),{"route":"answer","task_id":child})
        self.studio.wait(self.dm)
        self.assertEqual(len(self.studio.tasks(self.aid)),1)
        self.assertEqual(len(self.studio.tasks(self.dm)),1)
        self.assertEqual([e["text"] for e in target.chat() if e.get("during_task")],[change])
        def recap(messages):
            receipt=evidence(messages)["dispatches"][0]
            self.assertEqual((receipt["id"],receipt["status"]),(child,"completed"))
            self.assertEqual(receipt["file_changes"][0]["path"],"plan.md")
            return reply("Private delivered plan.md.")
        self.assertIn("plan.md",self.message("刚才派出去的交付了什么？",recap)["text"])

    def test_active_assistant_progress_steering_and_independent_commission(self):
        self.script.queues["assistant"] = [reply("",call("ask_human",question="Which region?")),reply("Local answer."),reply("Second job.")]
        text = "准备区域报告。"
        tid = self.message(text,action("start_room_task",text))["task_id"]
        question = wait_for(lambda:(self.studio.state(self.aid)["live"] or {}).get("question"))
        self.assertEqual(self.message("进行到哪了？",reply("在等地区。"))["route"],"reply")
        revise = "单位用千克。"
        self.assertEqual(self.message(revise,action("change_current_task",revise))["task_id"],tid)
        self.assertEqual(self.studio.state(self.aid)["live"]["question"],question)
        other = "另外整理一份术语表。"
        receipt = self.message(other,action("start_room_task",other))
        self.assertEqual(receipt["route"],"queued")
        self.assertNotEqual(receipt["task_id"],tid)
        self.message("用香港。",action("answer_open_question","香港"))
        self.studio.wait(self.aid)
        wait_for(lambda:self.studio.group(self.aid).load_task(receipt["task_id"])["status"]=="completed")
        calls = self.provider.for_agent("assistant")
        self.assertTrue(any(revise in m["content"] for m in calls[1]["messages"] if m["role"]=="user"))
        self.assertFalse(any("进行到哪了" in m["content"] for m in calls[1]["messages"] if m["role"]=="user"))

    def test_two_dispatches_require_a_target_and_a_stale_answer_cannot_reach_the_next_run(self):
        self.script.queues["analyst"] = [reply("",call("ask_human",question="Which date?")), reply("First closed."),
                                        reply("",call("ask_human",question="Which city?")), reply("Next closed.")]
        self.script.queues["editor"] = [reply("",call("ask_human",question="Which size?")),reply("Editor closed.")]
        _, first = self.dispatched(self.dm,"Ask Analyst for a date.")
        _, second = self.dispatched(self.team,"Ask the desk about size.")
        wait_for(lambda: (self.studio.state(self.dm)["live"] or {}).get("question"))
        wait_for(lambda: (self.studio.state(self.team)["live"] or {}).get("question"))
        incoming = "改成周一。"
        self.assertEqual(self.message(incoming, action("change_current_task",incoming))["route"],"reply")
        for gid in (self.dm,self.team):
            self.assertFalse(any(e.get("during_task") for e in self.studio.group(gid).chat()))
        event = reply("",call("change_current_task",task_id=second,quote=incoming))
        self.assertEqual(self.message(incoming,event),{"route":"steering","task_id":second})
        self.studio.post(self.dm,"Separate local job",queue=True)
        entered, resume = threading.Event(), threading.Event()
        def slow_answer(messages):
            self.assertEqual(evidence(messages)["action_targets"][first]["question"]["question"],"Which date?")
            entered.set()
            self.assertTrue(resume.wait(8))
            return reply("",call("answer_open_question",task_id=first,quote="周二"))
        with ThreadPoolExecutor() as pool:
            future = pool.submit(self.message,"采用周二", slow_answer)
            try:
                self.assertTrue(entered.wait(5))
                self.script.queues["room"] = [action("answer_open_question","Wednesday")]
                self.studio.post(self.dm,"Wednesday")
                wait_for(lambda: ((self.studio.state(self.dm)["live"] or {}).get("question") or {}).get("question")=="Which city?")
            finally:
                resume.set()
            self.assertEqual(future.result(5)["route"],"reply")
        self.assertEqual(self.studio.state(self.dm)["live"]["question"]["question"],"Which city?")
        self.assertEqual(self.studio.state(self.team)["live"]["question"]["question"],"Which size?")

    def test_receipts_survive_restart_and_only_authorize_the_original_task_snapshot(self):
        self.script.queues["analyst"] = [reply("",call("write_file",path="plan.md",content="first delivery")),reply("Original plan"),
                                         reply("",call("write_file",path="plan.md",content="private replacement")),reply("Private follow-up")]
        parent, child = self.dispatched(self.dm,"Write the original plan.")
        self.studio.wait(self.dm)
        self.studio.post(self.dm,"A separate private job",queue=True)
        self.studio.wait(self.dm)
        target = self.studio.group(self.dm)
        unrelated = target.tasks()[-1]["id"]
        self.studio.close()
        self.studio = Studio(self.studio.settings, lambda _: self.provider)
        self.addCleanup(self.studio.close)
        # Replay pre-routing storage as well. The assistant trace still holds the authentic start_task receipt.
        def consult():
            def inspect(messages):
                ctx = evidence(messages)
                self.assertEqual([d["id"] for d in ctx["dispatches"]],[child])
                self.assertNotIn("private replacement",json.dumps(ctx))
                return reply("",call("read_room_evidence",task_id=child,path="plan.md"),
                             call("read_room_evidence",task_id=unrelated,path="plan.md"))
            self.script.queues["?"] = [reply("Original plan: first delivery.")]
            result = self.message("上次派给 Analyst 的原件写的是什么？",inspect)
            self.assertEqual(result["route"],"reply")
            reads = [m["content"] for m in self.provider.for_agent("?")[-1]["messages"] if m["role"]=="tool"]
            self.assertEqual(reads,["first delivery","Task unavailable in this room."])
        consult()
        old = self.studio.group(self.aid).load_task(parent)
        old.pop("room_kind")
        self.studio.group(self.aid).save_task(old)
        record = target.load_task(child)
        record.pop("dispatched_by")
        target.save_task(record)
        consult()

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

    def test_older_dispatches_remain_available_on_demand(self):
        self.script.queues["analyst"] = [reply("Original result " + str(i)) for i in range(5)]
        children = []
        for i in range(5):
            _, child = self.dispatched(self.dm, "Work item " + str(i))
            self.studio.wait(self.dm)
            children.append(child)
        def request_receipt(messages):
            ctx = evidence(messages)
            self.assertEqual([e["id"] for e in ctx["earlier_dispatches"]],children[:1])
            self.assertNotIn("final_answer",ctx["earlier_dispatches"][0])
            return reply("",call("read_room_evidence",task_id=children[0]))
        def answer(messages):
            receipt = json.loads(messages[-1]["content"])
            self.assertEqual(receipt["final_answer"],"Original result 0")
            return reply(receipt["final_answer"])
        self.script.queues["?"] = [answer]
        self.assertEqual(self.message("第一项的结果呢？",request_receipt)["text"],"Original result 0")
        self.assertEqual(len(self.studio.tasks(self.aid)),5)

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
