"""Room conversation, execution and persistence through Studio's normal message entry."""
import hashlib
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from miniagent.studio import Studio
from tests.helpers import FakeProvider, Script, call, reply, settings
from tests.test_team import wait_for


def evidence(messages):
    return json.loads(messages[1]["content"].split("\n", 1)[1])


def action(name, user_words):
    return reply("", call(name, quote=user_words))


def file_hashes(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


class RoomTurnTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.script = Script()
        self.provider = FakeProvider(self.script)
        self.studio = Studio(settings(Path(temp.name)), lambda _: self.provider)
        self.addCleanup(self.studio.close)
        self.member = {"id":"editor", "name":"Editor", "prompt":"Edit project materials."}

    def room(self, name="Desk", kind="group"):
        return self.studio.create_group(name, [self.member], "editor", kind=kind)["id"]

    def test_followup_after_delivery_is_read_only_in_group_and_dm(self):
        for kind in ("group","dm"):
            with self.subTest(kind=kind):
                gid = self.room(kind, kind)
                text = "能做一份交接说明吗？"
                self.script.queues["room"] = [action("start_room_task", text)]
                self.script.queues["editor"] = [
                    reply("", call("write_file", path="handoff.md", content="Use SQLite.")),
                    reply("交付 handoff.md，采用 SQLite。")]
                tid = self.studio.post(gid, text, options={"checks":[{"kind":"exists","path":"handoff.md"}]})["task_id"]
                self.studio.wait(gid)
                store = self.studio.group(gid)
                before = file_hashes(store.root)
                def recap(messages):
                    facts = evidence(messages)
                    self.assertEqual(messages[-1]["content"], "你都做了啥")
                    last = facts["tasks"][-1]
                    self.assertEqual((last["id"], last["evaluation"]["passed"]), (tid,1))
                    self.assertEqual(last["file_changes"], [{"path":"handoff.md","author":"editor","change":"added"}])
                    return reply(last["final_answer"])
                self.script.queues["room"] = [recap]
                with patch.object(self.studio, "_start", side_effect=AssertionError("read-only turn started work")):
                    receipt = self.studio.post(gid, "你都做了啥")
                self.assertEqual(receipt, {"route":"reply","text":"交付 handoff.md，采用 SQLite。"})
                self.assertEqual([r["id"] for r in store.tasks()], [tid])
                after = file_hashes(store.root)
                self.assertEqual({k for k in before.keys() | after.keys() if before.get(k)!=after.get(k)}, {"chat.jsonl"})
                self.assertEqual(store.chat()[-1]["in_reply_to"], store.chat()[-2]["seq"])

    def test_same_message_depends_on_room_and_addressed_role(self):
        a, b = self.room("One","dm"), self.room("Two","dm")
        self.script.queues["room"] = [reply("We agreed to write the blue plan."), reply("The red plan already shipped.")]
        self.studio.post(a,"Discuss the blue plan.")
        self.studio.post(b,"Discuss the red plan.")
        incoming = "就按刚说的来。"
        def respond(messages):
            ctx = evidence(messages)
            if ctx["room"] == "One":
                self.assertIn("blue plan", ctx["conversation"])
                self.assertNotIn("red plan", str(ctx))
                return action("start_room_task", incoming)
            self.assertIn("red plan",ctx["conversation"])
            self.assertNotIn("blue plan", str(ctx))
            return reply("已确认上次交付。")
        self.script.queues["room"] = [respond, respond]
        self.script.queues["editor"] = [reply("finished")]
        self.assertEqual(self.studio.post(a, incoming)["route"], "task")
        self.studio.wait(a)
        self.assertIn("agreed to write the blue plan", self.provider.for_agent("editor")[-1]["messages"][1]["content"])
        self.assertEqual(self.studio.post(b, incoming)["route"], "reply")
        self.assertEqual(self.studio.tasks(b), [])

    def test_followup_steering_new_work_and_answer_while_question_is_open(self):
        gid = self.room()
        trigger = "帮我写草稿。"
        self.script.queues["editor"] = [reply("",call("ask_human",question="Choose a title.")),
            reply("revised draft"), reply("separate delivery")]
        self.script.queues["room"] = [action("start_room_task", trigger)]
        tid = self.studio.post(gid, trigger)["task_id"]
        store = self.studio.group(gid)
        question = wait_for(lambda:(self.studio.state(gid)["live"] or {}).get("question"))
        def recap(messages):
            ctx = evidence(messages)
            self.assertEqual(ctx["active_task"]["id"],tid)
            self.assertEqual(ctx["pending_question"]["id"],question["id"])
            return reply("正在等标题。")
        steer, extra, answer = "把篇幅减半，标题待定。", "另外做一份通讯录。", "标题用交接要点。"
        self.script.queues["room"] = [recap, action("change_current_task",steer),
                                     action("start_room_task",extra), action("answer_open_question",answer)]
        self.assertEqual(self.studio.post(gid,"目前卡在哪？")["route"],"reply")
        self.assertEqual(self.studio.post(gid,steer), {"route":"steering", "task_id":tid})
        self.assertEqual(self.studio.state(gid)["live"]["question"],question)
        queued = self.studio.post(gid,extra)
        self.assertEqual(queued["route"],"queued")
        self.assertEqual(self.studio.post(gid,answer),{"route":"answer","task_id":tid})
        wait_for(lambda: store.load_task(queued["task_id"])["status"]=="completed")
        worker_messages = self.provider.for_agent("editor")[1]["messages"]
        self.assertTrue(any(steer in m["content"] for m in worker_messages if m["role"]=="user"))
        self.assertFalse(any("目前卡在哪" in m["content"] for m in worker_messages if m["role"]=="user"))
        self.assertEqual([e["text"] for e in store.chat() if e["kind"]=="answer"],[answer])

    def test_stale_change_cannot_steer_the_next_task_and_other_room_remains_available(self):
        a, b = self.room("A"), self.room("B")
        worker, deciding, finish, commit = (threading.Event() for _ in range(4))
        def old_task(messages):
            worker.set()
            self.assertTrue(finish.wait(5))
            return reply("old task complete")
        def decision(messages):
            deciding.set()
            self.assertTrue(commit.wait(5))
            return action("change_current_task","改为周一")
        self.script.queues["editor"] = [old_task, reply("", call("ask_human", question="Which city?")), reply("ok")]
        self.script.queues["room"] = [decision, reply("独立会话正常。")]
        self.studio.post(a,"task one",queue=True)
        self.assertTrue(worker.wait(5))
        queued = self.studio.post(a,"task two",queue=True)
        with ThreadPoolExecutor() as pool:
            changing = pool.submit(self.studio.post,a,"改为周一")
            try:
                self.assertTrue(deciding.wait(5))
                self.assertEqual(self.studio.post(b,"可以聊吗？")["route"],"reply")
                finish.set()
                wait_for(lambda:(self.studio.state(a)["live"] or {}).get("question"))
            finally:
                finish.set()
                commit.set()
            self.assertEqual(changing.result(5)["route"],"reply")
        self.assertEqual(self.studio.state(a)["live"]["task_id"],queued["task_id"])
        self.assertEqual(self.studio.state(a)["live"]["question"]["question"],"Which city?")
        self.assertEqual(len(self.studio.tasks(a)),2)

    def test_followup_can_read_an_earlier_snapshot_but_cannot_access_another_room(self):
        gid = self.room()
        foreign = self.room("Other","dm")
        self.script.queues["editor"] = [reply("",call("write_file",path="draft.md",content="archived text")),reply("v1"),
            reply("",call("write_file",path="draft.md",content="replacement")),reply("v2"),
            reply("",call("write_file",path="secret.md",content="other room")),reply("done")]
        old = self.studio.post(gid,"First edition",queue=True)["task_id"]
        self.studio.wait(gid)
        self.studio.post(gid,"New edition",queue=True)
        self.studio.wait(gid)
        other = self.studio.post(foreign,"Private draft",queue=True)["task_id"]
        self.studio.wait(foreign)
        self.script.queues["room"] = [reply("", call("read_room_evidence",task_id=old,path="draft.md"),
                                             call("read_room_evidence",task_id=other,path="secret.md"))]
        def inspect_reads(messages):
            results = [m["content"] for m in messages if m["role"]=="tool"]
            self.assertEqual(results, ["archived text", "Task unavailable in this room."])
            return reply(results[0])
        self.script.queues["?"] = [inspect_reads]
        response = self.studio.post(gid,"初稿里写了什么？")
        self.assertEqual(response,{"route":"reply","text":"archived text"})
        self.assertEqual(len(self.studio.tasks(gid)),2)

    def test_mention_selects_the_speaker_without_commissioning_work(self):
        members = [self.member, {"id":"qa", "name":"Reviewer", "prompt":"Check releases."}]
        gid = self.studio.create_group("Team",members,"editor")["id"]
        def answer(messages):
            ctx = evidence(messages)
            self.assertEqual((ctx["recipient"],ctx["role"]),("qa","Check releases."))
            self.assertEqual([m["id"] for m in ctx["members"]], ["editor", "qa"])
            return reply("目前没有交付记录。")
        self.script.queues["room"] = [answer]
        self.assertEqual(self.studio.post(gid,"@qa 上次查到哪里？")["route"],"reply")
        self.assertEqual(self.studio.group(gid).chat()[-1]["agent"],"qa")
        self.assertEqual(self.studio.tasks(gid),[])

    def test_invalid_actions_cannot_launch_work(self):
        gid = self.room()
        msg = "可以解释一下吗？"
        self.script.queues["room"] = [action("start_room_task","旧的授权"), action("change_current_task",msg),
                                     reply("",call("run_shell",command="echo hello")), reply("truncated", finish="length"),
                                     reply("仍未完成", finish="length")]
        for _ in range(4):
            self.assertEqual(self.studio.post(gid,msg)["route"],"reply")
        self.assertEqual(self.studio.tasks(gid),[])
