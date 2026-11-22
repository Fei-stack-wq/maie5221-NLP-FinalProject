"""Exercise DeepSeek-formatted increments through the ordinary composer and a real browser."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

from miniagent.chat_stream import ChatStream
from miniagent.provider_http import HTTPResult, _sse_events
from miniagent.providers import DeepSeekProvider
from miniagent.room_turn import decide
from miniagent.server import serve
from miniagent.studio import Studio
from tests.helpers import FakeProvider, call, reply, settings


def chunk(delta, finish=None):
    frame = {"model":"deepseek-flash", "id":"offline-1",
             "choices":[{"index":0,"delta":delta,"finish_reason":finish}]}
    return ("data: "+json.dumps(frame,ensure_ascii=False)+"\n\n").encode()


class ChatStreamTest(unittest.TestCase):
    def test_reconnect_retry_and_two_simultaneous_replies(self):
        room = ChatStream(3)
        reader = room.frames()
        next(reader)
        with room.room_turn("lead","追问",[]) as send:
            send("delta","旧片段")
            room.fragment("t-1","lead","delta","任务")
            first = next(reader)
            self.assertEqual([p["text"] for p in first["drafts"]],["旧片段","任务"])
            send("discard","")
            send("delta","新")
            room.fragment("t-1","lead","delta","进度")
            second = next(reader)
            self.assertEqual(second["removed"],[first["drafts"][0]["id"]])
            self.assertEqual([p["text"] for p in second["drafts"]],["新","进度"])
            self.assertEqual([p["text"] for p in next(room.frames())["drafts"]],["新","任务进度"])
            room.committed({"seq":4,"kind":"report","task_id":"t-1","agent":"lead"})
        last = next(reader)
        self.assertEqual((last["after"],len(last["removed"]),last["drafts"]),(4,2,[]))

    def test_evidence_continuation_streams_and_failed_attempt_is_withdrawn(self):
        room = ChatStream(0)
        seen = []
        def respond(agent, messages, tools, options):
            send = options["on_stream"]
            if tools:
                send("delta","查阅中")
                return reply("查阅中",call("read_room_evidence",attachment_id="a1"))
            self.assertEqual(messages[-1]["content"],"verified")
            self.assertEqual(next(room.frames())["drafts"][0]["text"],"")
            send("delta","根据材料")
            seen.append(next(room.frames())["drafts"][0]["text"])
            return reply("根据材料")
        provider = FakeProvider(respond)
        provider.supports_streaming = True
        context = {"active_task":None,"pending_question":None,"attachments":[{"id":"a1"}],
                   "tasks":[],"kind":"group"}
        with room.room_turn("lead","聊聊材料",[]) as send:
            turn = decide(provider,context,"聊聊材料",lambda args:"verified",on_stream=send)
            self.assertEqual((turn.action, turn.text, seen),("reply","根据材料",["根据材料"]))
        with self.assertRaisesRegex(ValueError,"broken stream"):
            with room.room_turn("lead","再看看",[]) as send:
                send("delta","未完成")
                raise ValueError("broken stream")
        self.assertEqual(next(room.frames())["drafts"],[])

    def test_browser_receives_partial_before_direct_and_task_completion(self):
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
            key = "sk-test-only"
            studio = Studio(settings(Path(tmp), api_key=key),
                            lambda _: DeepSeekProvider(key,thinking="enabled"))
            main, preview = serve(studio,0)
            base = f"http://127.0.0.1:{main.server_port}/"
            assistant = studio.assistant()["id"]
            private = studio.create_group("私聊",[{"id":"editor","name":"编辑","prompt":"Write."}],
                                          "editor",kind="dm")["id"]
            group = studio.create_group("小组",[{"id":"lead","name":"负责人","prompt":"Help."}],"lead")["id"]
            chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
            try:
                with pw.chromium.launch(**({"executable_path":str(chrome)} if chrome.exists() else {})) as browser:
                    page = browser.new_page()
                    for gid, task in ((assistant,False),(group,False),(private,True)):
                        with self.subTest(gid=gid):
                            finish = threading.Event()
                            def post(url, payload, **options):
                                self.assertTrue(payload["stream"])
                                def packets():
                                    yield chunk({"reasoning_content":"private reason"})
                                    if task and any(t["function"]["name"]=="start_room_task" for t in payload["tools"]):
                                        yield chunk({"tool_calls":[{"index":0,"id":"call1","type":"function",
                                            "function":{"name":"start_room_task","arguments":json.dumps({"quote":"请写问候语"})}}]},
                                            "tool_calls")
                                    else:
                                        yield chunk({"content":"第一段。sk-test"})
                                        yield chunk({"content":"-only"})
                                        if not finish.wait(20):
                                            raise AssertionError("Browser did not receive the first fragment")
                                        yield chunk({"content":"第二段。"},"stop")
                                    yield b"data: [DONE]\n\n"
                                return HTTPResult(options["stream_decoder"](_sse_events(packets(),1)),1)
                            with patch("miniagent.providers.post_json",side_effect=post):
                                try:
                                    page.goto(base+"#"+gid)
                                    page.locator("#input").fill("请写问候语" if task else "聊一聊")
                                    page.locator("#composer button[type=submit]").click()
                                    partial = page.locator("#timeline .chat-draft .msg:not(.user) .draft-text")
                                    expect(partial).to_have_text("第一段。[redacted]")
                                    self.assertFalse(any(e["kind"] in ("final","room_reply") for e in studio.group(gid).chat()))
                                    if gid == assistant:
                                        page.reload()
                                        expect(partial).to_have_text("第一段。[redacted]")
                                        other = browser.new_page()
                                        other.goto(base+"#"+private)
                                        expect(other.locator("#room-name")).to_contain_text("私聊")
                                        expect(other.locator(".chat-draft")).to_have_count(0)
                                        other.close()
                                finally:
                                    finish.set()
                                expect(page.locator("#timeline .msg .body").get_by_text(
                                    "第一段。[redacted]第二段。",exact=True)).to_have_count(1)
                                expect(page.locator(".chat-draft")).to_have_count(0)
                                self.assertEqual(len(studio.tasks(gid)),int(task))
                                log = (studio.group(gid).root/"chat.jsonl").read_text()
                                self.assertNotIn("private reason",log)
                                self.assertNotIn(key,log)
            finally:
                studio.close()
                for server in (main,preview):
                    server.shutdown()
                    server.server_close()
