"""Replay the reported messages at the provider boundary without a live account."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from miniagent.provider_http import HTTPResult, ProviderError
from miniagent.providers import DeepSeekProvider, _collect_deepseek_stream
from miniagent.studio import Studio
from tests.helpers import FakeProvider, Script, call, reply, settings
from tests.test_team import wait_for


def markup(name, body):
    tag = "｜｜DSML｜｜ "
    params = "".join(f'<{tag}parameter name="{k}" string="{str(isinstance(v,str)).lower()}">{v}</{tag}parameter>'
                     for k,v in body.items())
    return f'<{tag}calls><{tag}invoke name="{name}">{params}</{tag}invoke></{tag}calls>'


def completion(content=None, tool=None):
    msg = {"role":"assistant","content":content,"reasoning_content":"reason"}
    if tool:
        msg["tool_calls"] = [{"type":"function","id":"test-call","function":{
            "name":tool[0], "arguments":json.dumps(tool[1],ensure_ascii=False)}}]
    return HTTPResult({"choices":[{"message":msg, "finish_reason":"tool_calls" if tool else "stop"}],
                       "usage":{"prompt_tokens":10,"completion_tokens":3}},1)


class ProtocolTest(unittest.TestCase):
    def test_control_marker_split_at_every_boundary_and_native_precedence(self):
        raw = markup("cancel_task", {"task_id":"t-000000000001"})
        for split in range(len(raw)+1):
            seen = []
            events = [{"choices":[{"delta":{"content":part},"finish_reason":finish}]} for part,finish in
                      ((raw[:split],None),(raw[split:],"stop"))]
            data = _collect_deepseek_stream(iter(events),lambda k,v:seen.append(v),None)
            self.assertEqual("".join(seen),"")
            self.assertEqual(data["choices"][0]["message"]["content"],raw)
        provider = DeepSeekProvider("synthetic")
        native = completion(raw, ("list_groups",{}))
        self.assertEqual([c.name for c in provider._parse(native.data,0).tool_calls],["list_groups"])
        self.assertFalse(provider._parse(native.data,0).text)
        with patch.object(provider,"_post_with_retry",return_value=completion(raw)) as send:
            with self.assertRaises(ProviderError) as error:
                provider.chat([{"role":"user","content":"Explain this example"}])
            self.assertEqual((error.exception.code,error.exception.attempts,send.call_count),("invalid_tool_markup",2,2))

    def test_native_repair_counts_usage_and_preserves_original_history(self):
        provider = DeepSeekProvider("synthetic")
        history = [{"role":"user","content":"可以聊聊吗"}]
        with patch.object(provider,"_post_with_retry",side_effect=[
                completion(markup("start_room_task",{"quote":"可以聊聊吗"})),completion("可以，一起看看。")]):
            response = provider.chat(history)
        self.assertEqual((response.text,response.request_attempts,response.usage.total),("可以，一起看看。",2,26))
        self.assertEqual(history,[{"role":"user","content":"可以聊聊吗"}])


class SupervisorTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.script = Script(default=lambda *_:reply("已交付"))
        self.fake = FakeProvider(self.script)
        self.studio = Studio(settings(self.root/"runs"), lambda _:self.fake)
        self.addCleanup(self.studio.close)
        self.assistant = self.studio.assistant()["id"]
        self.rooms = [self.studio.create_group(name,[{"id":"lead","name":"负责人","prompt":"完成工作"}],"lead")["id"]
                      for name in ("编辑","软件","投资")]

    def replay(self, room, text, frames):
        provider = DeepSeekProvider("synthetic")
        real_factory = self.studio.provider_factory
        # Only the fast chat decision uses the recorded DeepSeek HTTP envelopes.
        self.studio.provider_factory = lambda _:(provider if frames else self.fake)
        def take(*args,**kwargs):
            return frames.pop(0)
        try:
            with patch.object(provider,"_post_with_retry",side_effect=take):
                return self.studio.post(room,text)
        finally:
            self.studio.provider_factory = real_factory

    def test_editor_continuation_read_then_independent_routing(self):
        room = self.studio.group(self.rooms[0])
        text = "继续task2"
        room.append("room_reply", agent="lead", text="task2 要在既有材料上交付核查报告。")
        folder = self.root/"drafts"
        folder.mkdir()
        (folder/"record.md").write_text("a"*32768+"既有事实")
        self.studio.update_group(room.group.id,{"folder":str(folder)})
        args = {"workspace_path":"record.md","offset":32768}
        raw = markup("read_room_evidence",args)
        room.append("room_reply", agent="lead", text=raw)
        self.assertNotIn("DSML",room.chat()[-1]["text"])
        self.assertIn("DSML",(room.root/"chat.jsonl").read_text())
        frames = [completion(raw),completion(tool=("read_room_evidence",args)),
                  completion(tool=("start_room_task",{"quote":text})),
                  completion(tool=("start_room_task",{"quote":text}))]
        result = self.replay(room.group.id,text,frames)
        self.studio.wait(room.group.id)
        self.assertEqual((result["route"],len(room.tasks()),len(frames)),("task",1,0))
        self.assertIn("核查报告",self.fake.for_agent("lead")[0]["messages"][1]["content"])

    def test_three_approvals_replay_once_and_tolerate_partial_delivery(self):
        text = "那你下发吧 三个都开"
        source = self.studio.group(self.assistant)
        source.append("room_reply",agent="assistant",text="可以把 task2 分给编辑、软件、投资三个组。")
        jobs = [{"group_id":gid,"text":"继续该组的 task2"} for gid in self.rooms]
        partial = [jobs[0],{"group_id":"missing","text":"bad"},jobs[2]]
        self.script.queues["assistant"] = [reply("",call("dispatch_tasks",assignments=partial)),
                                            reply("",call("dispatch_tasks",assignments=jobs))]
        self.replay(self.assistant,text,[completion(markup("start_room_task",{"quote":text})),
                                        completion(tool=("start_room_task",{"quote":text}))])
        self.studio.wait(self.assistant)
        for gid in self.rooms:
            self.studio.wait(gid)
            self.assertEqual(len(self.studio.tasks(gid)),1)
        events=source.chat()
        for kind in ("dispatch_sent","dispatch_finished"):
            self.assertEqual({e["target_group_id"] for e in events if e["kind"]==kind},set(self.rooms))
            self.assertEqual(sum(e["kind"]==kind for e in events),3)
        self.assertNotIn("DSML",json.dumps(events,ensure_ascii=False))

    def test_cancel_queued_and_transfer_original_attachment_without_stopping_successor(self):
        gid, destination, _ = self.rooms
        source = self.studio.group(gid)
        self.script.queues["lead"] = [reply("", call("ask_human",question="Which edition?")), reply("Done"), reply("Delivered")]
        original = self.studio.post(gid,"First",queue=True)["task_id"]
        wait_for(lambda:(self.studio.state(gid)["live"] or {}).get("question"))
        ref=self.studio.uploads.save_in_room(source,"facts.txt",b"original inputs")
        queued = self.studio.post(gid,"Use facts",attachments=[ref],queue=True)["task_id"]
        self.studio.post(gid,"Third",queue=True)
        target=self.studio.create_session(destination,"第二期")["id"]
        self.script.queues["room"]=[reply("",call("start_room_task",quote="重新派发"))]
        args={"task_id":queued,"group_id":target}
        self.script.queues["assistant"]=[reply("",call("redispatch_task",**args)),
                                         reply("",call("redispatch_task",**args)),reply("转交完成")]
        self.studio.post(self.assistant,"重新派发")
        self.studio.wait(self.assistant)
        self.studio.wait(target)
        items=self.studio.tasks(target)
        self.assertEqual([(t["text"],t["redispatched_from"]) for t in items],[("Use facts",queued)])
        room=self.studio.group(target)
        copied=next(e["attachments"][0] for e in room.chat() if e.get("attachments"))
        self.assertEqual((copied["session_id"],self.studio.uploads.original(room,copied).read_bytes()),(target,b"original inputs"))
        self.assertEqual((source.load_task(queued)["status"],source.load_task(original)["status"]),("cancelled","running"))
        self.script.queues["room"]=[reply("",call("cancel_task",task_id=original,quote="停掉第一项"))]
        self.assertIn("stopping",self.studio.post(self.assistant,"停掉第一项")["text"])
        wait_for(lambda:all(t["status"] not in ("queued","running") for t in source.tasks()))
        self.assertEqual([r["status"] for r in source.tasks()],["cancelled","cancelled","completed"])
        self.script.queues["room"]=[reply("",call("cancel_task",task_id=original,quote="再停"))]
        self.assertIn("cancelled",self.studio.post(self.assistant,"再停")["text"])
        for request in self.fake.for_agent("lead"):
            self.assertNotIn("cancel_task",request["tools"])

    def test_rejected_target_leaves_source_in_place_and_attached_steering_is_readable(self):
        source_id, target, _ = self.rooms
        self.script.queues["lead"] = [reply("",call("ask_human",question="Title?")),reply("Finished")]
        tid = self.studio.post(source_id,"Original assignment",queue=True)["task_id"]
        source = self.studio.group(source_id)
        wait_for(lambda:(self.studio.state(source_id)["live"] or {}).get("question"))
        self.studio.update_group(target,{"archived":True})
        self.script.queues["room"] = [reply("",call("start_room_task",quote="重派")),
                                      reply("",call("change_current_task",task_id=tid,quote="附上材料")),
                                      reply("",call("answer_open_question",task_id=tid,quote="香港"))]
        self.script.queues["assistant"] = [reply("",call("redispatch_task",task_id=tid,group_id=target)),
                                           reply("目标已归档，原任务仍在。")]
        self.studio.post(self.assistant,"重派")
        self.studio.wait(self.assistant)
        self.assertEqual(self.studio.tasks(target),[])
        self.assertEqual(source.load_task(tid)["status"],"running")
        ref = self.studio.uploads.save_in_room(self.studio.group(self.assistant),"update.txt",b"late steer")
        self.assertEqual(self.studio.post(self.assistant,"附上材料",attachments=[ref])["route"],"steering")
        copied = next(e["attachments"][0] for e in source.chat() if e.get("during_task"))
        self.assertEqual(self.studio.uploads.original(source,copied).read_bytes(),b"late steer")
        self.studio.post(self.assistant,"香港")
        self.studio.wait(source_id)
        self.assertEqual(source.load_task(tid)["status"],"completed")

    def test_settings_and_directory_through_chat(self):
        team=self.rooms[0]
        session=self.studio.create_session(team,"一期")["id"]
        folder=self.root/"project"; folder.mkdir()
        for gid, change in ((team,{"name":"新编辑部","pinned":True,"mode":"autonomous"}),
                            (session,{"name":"二期","folder":str(folder)})):
            self.script.queues["room"]=[reply("",call("update_group_settings",group_id=gid,quote="调整设置",**change))]
            self.assertIn("已更新",self.studio.post(self.assistant,"调整设置")["text"])
        self.script.queues.update(room=[reply("",call("inspect_studio_room",group_id=session)),
                                  lambda msgs:reply(json.loads(msgs[-1]["content"])["workspace_path"])])
        self.assertEqual(self.studio.post(self.assistant,"二期的目录在哪里？")["text"],str(folder))
        self.assertEqual(self.studio.tasks(self.assistant),[])
        self.assertEqual(self.studio.group(team).group.folder,"")
