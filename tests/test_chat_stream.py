"""Exercise DeepSeek-formatted increments through the ordinary composer and a real browser."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.request
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

from miniagent.chat_stream import ChatStream
from miniagent.provider_http import HTTPResult, ProviderError, _sse_events
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

    def test_click_to_reply_over_slow_http_including_thinking_and_multiple_fragments(self):
        # Real sockets on both hops. The test controls when the provider can release each part.
        public = threading.Event()
        more = threading.Event()
        finish = threading.Event()
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_POST(self):
                payload=json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                assert payload["stream"] and payload["thinking"]["type"]=="enabled"
                self.send_response(200)
                self.send_header("Content-Type","text/event-stream")
                self.end_headers()
                def send(data):
                    self.wfile.write(data)
                    self.wfile.flush()
                send(chunk({"reasoning_content":"PRIVATE-REASON"}))
                assert public.wait(20)
                send(chunk({"content":"一片"}))
                assert more.wait(20)
                send(chunk({"content":"两片"}))
                assert finish.wait(20)
                send(chunk({"content":"三片"},"stop"))
                send(b"data: [DONE]\n\n")
        upstream=ThreadingHTTPServer(("127.0.0.1",0),Upstream)
        threading.Thread(target=upstream.serve_forever,daemon=True).start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        class LocalProviderTransport:
            def open(self, request, timeout):
                local = urllib.request.Request(f"http://127.0.0.1:{upstream.server_port}/",data=request.data)
                return opener.open(local, timeout=timeout)
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
            studio=Studio(settings(Path(tmp),api_key="offline-test-key",thinking="enabled"))
            main,preview=serve(studio,0)
            gid=studio.assistant()["id"]
            try:
                chrome=Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
                with pw.chromium.launch(**({"executable_path":str(chrome)} if chrome.exists() else {})) as browser, \
                        patch("miniagent.provider_http.urllib.request.build_opener",return_value=LocalProviderTransport()):
                    page=browser.new_page(viewport={"width":1440,"height":900}, reduced_motion="reduce")
                    page.goto(f"http://127.0.0.1:{main.server_port}/#{gid}")
                    requests=[]
                    page.route("**/messages",lambda route:requests.append(route))
                    page.locator("#input").fill("这是一个追问")
                    with page.expect_request(lambda request:request.url.endswith("/messages")):
                        page.locator("#composer button[type=submit]").click()
                    expect(page.locator("#input")).to_have_value("")
                    expect(page.locator("#timeline .msg.user")).to_contain_text("这是一个追问")
                    expect(page.locator(".welcome")).to_have_count(0)
                    self.assertEqual(len(requests),1)
                    page.evaluate("window.sentBubble = document.querySelector('.outgoing')")
                    requests.pop().continue_()
                    expect(page.locator(".reply-phase")).to_have_text("正在思考…")
                    self.assertNotIn("PRIVATE-REASON",page.locator("#timeline").inner_text())
                    expect(page.locator("#timeline .msg.user")).to_have_count(1)
                    page.locator("#input").fill("下一条草稿")
                    public.set()
                    body=page.locator(".chat-draft .msg:not(.user) .draft-text")
                    expect(body).to_have_text("一片")
                    def edges():
                        return page.evaluate("""() => Object.fromEntries(['#timeline .msg.user',
                            '#timeline .msg:not(.user)', '#composer', '#input'].map(selector => {
                            const r=document.querySelector(selector).getBoundingClientRect();
                            // The user bubble gains its timestamp on commit and grows toward the left.
                            return [selector,selector.endsWith('.user') ? r.right : [r.left,r.right]];
                        }))""")
                    def layouts():
                        result = {}
                        for width in (390,1440):
                            page.set_viewport_size({"width":width,"height":900})
                            result[width] = edges()
                        return result
                    partial_layout = layouts()
                    more.set()
                    expect(body).to_have_text("一片两片")
                    self.assertFalse(any(e["kind"]=="room_reply" for e in studio.group(gid).chat()))
                    # Durable polling can win the race with the next SSE event. Delay that
                    # event, then replay even the old full draft as a stale connection might.
                    page.evaluate("""() => {
                        window.replyBubble = document.querySelector('.chat-draft .msg:not(.user)');
                        const feed=state.feed, receive=feed.onframe;
                        const draft=[...feed.items.entries()][0];
                        const replay={id:draft[0],agent:"assistant",client_id:draft[1].clientId,
                            request:"这是一个追问",attachments:[],text:"一片两片",phase:"streaming"};
                        feed.onframe=()=>{};
                        window.lateFrame=()=>receive({data:JSON.stringify({drafts:[replay],removed:[],after:state.after})});
                    }""")
                    finish.set()
                    expect(page.locator("#timeline .body").get_by_text("一片两片三片",exact=True)).to_have_count(1)
                    expect(page.locator(".chat-draft")).to_have_count(0)
                    expect(page.locator("#timeline .msg.user")).to_have_count(1)
                    expect(page.locator("#input")).to_have_value("下一条草稿")
                    self.assertEqual(layouts(), partial_layout)
                    page.evaluate("lateFrame()")
                    self.assertTrue(page.evaluate("""() => document.querySelector('#timeline .msg.user')===sentBubble &&
                        document.querySelector('#timeline .msg:not(.user)')===replyBubble"""))
                    expect(page.locator("#timeline .msg")).to_have_count(2)
                    user=next(e for e in studio.group(gid).chat() if e["kind"]=="user_message")
                    again=studio.post(gid,"这是一个追问",client_id=user["client_id"])
                    self.assertEqual(again["seq"],user["seq"])
                    self.assertEqual(len(studio.tasks(gid)),0)
            finally:
                public.set(); more.set(); finish.set()
                studio.close()
                for server in (main,preview,upstream):
                    server.shutdown()
                    server.server_close()

    def test_followup_while_sending_and_inline_retry_keep_input_and_attachment(self):
        gate=threading.Event()
        seen=[]
        def respond(agent,messages,tools,options):
            text=messages[-1]["content"]
            seen.append(text)
            if len(seen)==1:
                if not gate.wait(15): raise AssertionError("Second send did not queue")
                raise ProviderError("temporary failure",code="unavailable")
            return reply("已回复"+text)
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
            studio=Studio(settings(Path(tmp)),lambda _:FakeProvider(respond))
            main,preview=serve(studio,0)
            gid=studio.assistant()["id"]
            try:
                chrome=Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
                with pw.chromium.launch(**({"executable_path":str(chrome)} if chrome.exists() else {})) as browser:
                    page=browser.new_page()
                    page.goto(f"http://127.0.0.1:{main.server_port}/#{gid}")
                    page.locator("#uploads").set_input_files({"name":"note.txt","mimeType":"text/plain","buffer":b"hello"})
                    page.locator("#input").fill("第一条")
                    page.locator("#input").press("Enter")
                    expect(page.locator(".reply-phase")).to_have_text("正在处理…")
                    page.locator("#input").fill("第二条")
                    page.locator("#input").press("Enter")
                    expect(page.locator(".outgoing")).to_have_count(2)
                    expect(page.locator("#input")).to_have_value("")
                    self.assertEqual(seen,["第一条"])
                    page.locator("#input").fill("我还在写")
                    gate.set()
                    retry=page.get_by_role("button",name="重试",exact=True)
                    expect(retry).to_be_visible()
                    expect(page.locator("#timeline")).to_contain_text("已回复第二条")
                    retry.click()
                    expect(page.locator("#timeline")).to_contain_text("已回复第一条")
                    expect(page.locator(".outgoing")).to_have_count(0)
                    expect(page.locator("#input")).to_have_value("我还在写")
                    records=[e for e in studio.group(gid).chat() if e["kind"]=="user_message"]
                    self.assertEqual([r["text"] for r in records],["第二条","第一条"])
                    self.assertEqual(records[1]["attachments"][0]["name"],"note.txt")
                    self.assertEqual(len({r["client_id"] for r in records}),2)
                    studio.group(gid).append("final",agent="assistant",status="completed",text="已交付。")
                    held=[]
                    page.route("**/messages",lambda route:held.append(route))
                    with page.expect_request(lambda request:request.url.endswith("/messages")):
                        page.get_by_role("button",name="接受交付",exact=True).click()
                    expect(page.locator(".outgoing")).to_contain_text("我接受这次交付。")
                    expect(page.locator("#input")).to_have_value("我还在写")
                    held.pop().continue_()
                    expect(page.locator("#timeline")).to_contain_text("已回复我接受这次交付。")
                    expect(page.locator(".outgoing")).to_have_count(0)
                    expect(page.get_by_role("button",name="已接受交付")).to_be_disabled()
                    page.reload()
                    expect(page.get_by_role("button",name="已接受交付")).to_be_disabled()
            finally:
                gate.set(); studio.close()
                for server in (main,preview):
                    server.shutdown()
                    server.server_close()

    def test_older_service_renders_one_copy_without_a_stream_or_receipt_id(self):
        gate = threading.Event()
        def respond(*args):
            if not gate.wait(15):
                raise AssertionError("Browser did not observe the pending send")
            return reply("旧服务回复")
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
            studio = Studio(settings(Path(tmp)), lambda _: FakeProvider(respond))
            main, preview = serve(studio, 0)
            room = studio.assistant()["id"]
            try:
                chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
                with pw.chromium.launch(**({"executable_path":str(chrome)} if chrome.exists() else {})) as browser:
                    page = browser.new_page()
                    def legacy_bootstrap(route):
                        response = route.fetch()
                        values = response.json()
                        del values["chat_protocol"]
                        route.fulfill(response=response, json=values)
                    def legacy_post(route):
                        payload = route.request.post_data_json
                        payload.pop("client_id", None)
                        route.continue_(post_data=json.dumps(payload))
                    page.route("**/api/bootstrap", legacy_bootstrap)
                    page.route("**/messages", legacy_post)
                    stream_requests = []
                    page.on("request", lambda r: stream_requests.append(r.url) if r.url.endswith("/chat-stream") else None)
                    page.goto(f"http://127.0.0.1:{main.server_port}/#{room}")
                    box = page.locator("#input")
                    expect(page.locator("#composer")).to_contain_text("只支持整段回复")
                    box.fill("相同的正文"); box.press("Enter")
                    expect(page.locator("#timeline")).to_contain_text("正在等待完整回复")
                    gate.set()
                    expect(page.locator("#timeline .msg.user")).to_have_count(1)
                    expect(page.locator("#timeline .outgoing")).to_have_count(0)
                    # The user can intentionally send identical text as a different message.
                    box.fill("相同的正文"); box.press("Enter")
                    expect(page.locator("#timeline .msg.user")).to_have_count(2)
                    expect(page.locator("#timeline .outgoing")).to_have_count(0)
                    expect(page.locator("#timeline .msg:not(.user)")).to_have_count(2)
                    self.assertFalse(stream_requests)
                    self.assertFalse(any(e.get("client_id") for e in studio.group(room).chat()))
            finally:
                gate.set(); studio.close()
                for server in (main,preview):
                    server.shutdown()
                    server.server_close()

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
