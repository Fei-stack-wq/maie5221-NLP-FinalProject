"""Mid-handoff interjections through the ordinary Studio entry, with offline providers."""
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from miniagent.config import TeamLimits
from miniagent.provider_http import ProviderError, _interruptible_reads
from miniagent.providers import DeepSeekProvider
from miniagent.studio import Studio
from tests.helpers import FakeProvider, call, reply, settings, tool_results
from tests.test_team import TEAM, finished, wait_for


class RoomFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def start(self, respond, *, stream=False, **flags):
        self.provider = FakeProvider(respond)
        self.provider.supports_streaming = stream
        switches = {"mailboxes":True, "interrupts":True, "lead_consult":True, "retrospective":False, **flags}
        self.studio = Studio(settings(Path(self.tmp.name), team=TeamLimits(**switches)), lambda _:self.provider)
        self.addCleanup(self.studio.close)
        self.room = self.studio.create_group("Delivery", TEAM, "lead", "autonomous")["id"]
        self.store = self.studio.group(self.room)
        self.tid = self.studio.post(self.room, "Build", queue=True)["task_id"]

    def rows(self, kind):
        return [e for e in self.store.chat() if e["kind"] == kind]

    @staticmethod
    def steer(messages):
        return reply("", call("change_current_task", quote=messages[-1]["content"]))



class MailboxTest(RoomFixture):
    def test_addressed_tool_interrupt_closes_batch_and_preserves_following_work(self):
        def respond(agent, m, tools, opts):
            if agent == "room":
                return self.steer(m)
            if agent == "lead":
                if not tool_results(m):
                    return reply("", call("delegate_task", assignee="dev", task="Prepare the delivery."))
                return reply("Delivered.")
            if not tool_results(m):
                return reply("", call("run_shell", command="touch started; sleep 30; touch stale"),
                             call("write_file", path="stale.md", content="old"))
            DeepSeekProvider._validate_history(m, require_reasoning=False)
            return reply("Changed." if any("change now" in str(r["content"]) for r in m if r["role"]=="user") else "Missing.")
        self.start(respond)
        work = self.store.workspace("dev")
        wait_for(lambda:(work/"started").exists())
        began = time.monotonic()
        result = self.studio.post(self.room, "@dev change now", urgent=True)
        self.assertEqual(result["route"], "steering")
        wait_for(lambda:self.rows("mail_receipt"))
        self.assertLess(time.monotonic()-began, 2.0)
        task = finished(self.studio, self.room)
        self.assertEqual(task["status"], "completed")
        self.assertTrue(self.rows("mail_receipt")[0]["interrupted"])
        self.assertEqual(self.rows("mail_receipt")[0]["agent"], "dev")
        self.assertEqual(self.rows("mail")[0]["recipients"], ["dev"])
        self.assertEqual(self.rows("report")[0]["text"], "Changed.")
        self.assertFalse((work/"stale").exists() or (work/"stale.md").exists())
        results = tool_results(self.provider.for_agent("dev")[-1]["messages"])
        self.assertEqual(len(results), 2)
        self.assertTrue(all("Interrupted by a new message" in text for text in results))
        self.assertFalse(any("change now" in str(r["content"]) for r in self.provider.for_agent("lead")[-1]["messages"]
                             if r["role"] == "user"))

    def test_pause_during_interrupted_tool_does_not_replay_the_command_or_redeliver_mail(self):
        finishing = threading.Event()
        def respond(agent,m,tools,opts):
            if agent == "room":
                return self.steer(m)
            if agent == "lead":
                return reply("Delivered") if tool_results(m) else reply("",call("delegate_task",assignee="dev",task="Check work."))
            if not tool_results(m):
                return reply("", call("run_shell", command="echo run >> attempts; sleep 30"))
            if not finishing.is_set():
                wait_for(opts["should_cancel"])
                raise ProviderError("pause",code="cancelled")
            DeepSeekProvider._validate_history(m,require_reasoning=False)
            return reply("Handled " + str(sum("new requirement" in str(r["content"]) for r in m if r["role"]=="user")))
        self.start(respond,stream=True)
        wait_for(lambda:(self.store.workspace("dev")/"attempts").exists())
        self.studio.post(self.room,"@dev new requirement",urgent=True)
        self.studio.pause_task(self.tid)
        wait_for(lambda:self.store.load_task(self.tid)["status"]=="paused")
        finishing.set()
        self.studio.resume_task(self.tid)
        self.assertEqual(finished(self.studio,self.room)["status"],"completed")
        self.assertEqual((self.store.workspace("dev")/"attempts").read_text(),"run\n")
        self.assertEqual(self.rows("report")[0]["text"],"Handled 1")
        self.assertEqual([r["agent"] for r in self.rows("mail_receipt")],["dev"])

    def test_stream_interruption_discards_partial_and_refunds_step(self):
        first = threading.Event()
        def respond(agent, m, tools, opts):
            if agent == "room":
                return self.steer(m)
            if not first.is_set():
                opts["on_stream"]("delta", "wrong draft")
                first.set()
                wait_for(opts["should_cancel"])
                raise ProviderError("interrupted", code="cancelled")
            self.assertFalse(any("wrong draft" in str(row.get("content")) for row in m))
            self.assertIn("instead", m[-1]["content"])
            DeepSeekProvider._validate_history(m, require_reasoning=False)
            return reply("New draft.")
        self.start(respond, stream=True)
        self.assertTrue(first.wait(5))
        self.studio.post(self.room,"@lead instead",urgent=True)
        result = finished(self.studio, self.room)
        self.assertEqual(result["metrics"]["lead"]["steps"], 1)
        self.assertEqual(result["final_answer"], "New draft.")
        self.assertEqual(sum(e["interrupted"] for e in self.rows("mail_receipt")), 1)

    def test_broadcast_reaches_active_agents_new_member_reads_it_once_from_context(self):
        release = threading.Event()
        entered = threading.Event()
        def respond(agent, m, tools, opts):
            if agent == "room":
                return self.steer(m)
            received = tool_results(m)
            if agent == "lead":
                if len(received) < 2:
                    return reply("", call("delegate_task",assignee=("dev","qa")[len(received)],task="Make an independent check."))
                return reply("Complete.")
            if agent == "dev" and not entered.is_set():
                entered.set()
                release.wait(5)
                return reply("Finished after adjustment.")
            return reply("Note received.")
        self.start(respond, lead_consult=False)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(5))
        self.studio.post(self.room, "Use the blue fixture")
        release.set()
        finished(self.studio, self.room)
        self.assertEqual({e["agent"] for e in self.rows("mail_receipt")}, {"dev","lead"})
        qa = self.provider.for_agent("qa")[0]["messages"]
        self.assertEqual(sum(str(r.get("content")).count("Use the blue fixture") for r in qa),1)

    def test_urgent_cooldown_is_per_recipient_and_does_not_drop_mail(self):
        release = threading.Event()
        second = threading.Event()
        first = threading.Event()
        def respond(agent,m,tools,opts):
            if agent=="room":
                return self.steer(m)
            if not first.is_set():
                first.set()
                wait_for(opts["should_cancel"])
                raise ProviderError("interrupted", code="cancelled")
            if not second.is_set():
                second.set()
                release.wait(5)
                self.assertFalse(opts["should_cancel"]())
            return reply("Done")
        self.start(respond,stream=True)
        self.addCleanup(release.set)
        self.assertTrue(first.wait(5))
        self.studio.post(self.room,"first change",urgent=True)
        self.assertTrue(second.wait(5))
        self.studio.post(self.room,"next change",urgent=True)
        release.set()
        finished(self.studio,self.room)
        self.assertEqual([r["interrupted"] for r in self.rows("mail_receipt")], [True,False])



class ConsultTest(RoomFixture):
    def test_all_lead_actions_and_unparseable_fallback(self):
        for action in ("forward","forward_urgent","stop_member","reply","hold","malformed"):
            with self.subTest(action=action):
                entered, release = threading.Event(), threading.Event()
                self.addCleanup(release.set)

                def respond(agent,m,tools,opts):
                    if agent == "room":
                        return self.steer(m)
                    if agent == "lead":
                        if m[-1]["content"].startswith("Decide as the task lead"):
                            DeepSeekProvider._validate_history(m, require_reasoning=False)
                            self.assertTrue(any("(still running: dev" in str(row["content"]) for row in m))
                            return reply("unparsable" if action == "malformed" else json.dumps({
                                "action":action,"to":["dev"],"text":"Updated instructions."}))
                        return reply("Delivered.") if tool_results(m) else reply("",call("delegate_task",assignee="dev",
                                                                                       task="Carry out QA."))
                    entered.set()
                    release.wait(5)
                    return reply("Member report.")
                self.start(respond)
                self.assertTrue(entered.wait(5))
                self.studio.post(self.room,"@Tech Lead update verification")
                decision = wait_for(lambda: next((r for r in self.rows("mail") if r.get("decided_for") is not None),None))
                self.assertEqual(decision["action"], "forward" if action == "malformed" else action)
                self.assertEqual(decision["fallback"], action == "malformed")
                self.assertEqual(decision["reply_to"],self.rows("mail")[0]["id"])
                forward = [r for r in self.rows("mail") if r["recipients"] == ["dev"]]
                if action in ("reply","hold"):
                    self.assertEqual(forward, [])
                else:
                    self.assertEqual(forward[0]["urgent"], action in ("forward_urgent","stop_member"))
                    if action == "malformed":
                        self.assertIn("update verification",forward[0]["text"])
                if action == "reply":
                    self.assertEqual(self.rows("room_reply")[-1]["text"], "Updated instructions.")
                release.set()
                result = finished(self.studio,self.room)
                self.assertEqual(result["status"],"completed")
                self.assertTrue(any("Decision you made" in str(r["content"])
                                    for r in self.provider.for_agent("lead")[-1]["messages"]))
                trace = [json.loads(line) for line in (self.store.task_dir(self.tid)/"trace.jsonl").read_text().splitlines()]
                self.assertEqual(sum(t.get("purpose")=="consult" for t in trace),1)
                self.studio.close()

    def test_mentions_override_model_targets_and_fast_consult_budget(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def respond(agent,m,tools,opts):
            if agent=="room":
                return reply("",call("change_current_task",quote=m[-1]["content"],recipients=["qa"]))
            if agent=="lead":
                if m[-1]["content"].startswith("Decide as the task lead"):
                    return reply('{"action":"hold","to":[],"text":"Waiting for the report"}')
                return reply("Done") if tool_results(m) else reply("",call("delegate_task",assignee="dev",task="Inspect work."))
            entered.set()
            release.wait(8)
            return reply("Checked")
        self.start(respond, fast=True)
        self.assertTrue(entered.wait(5))
        for n in range(4):
            self.studio.post(self.room,f"@TECH LEAD, @Developer revision {n}")
        wait_for(lambda:sum(r["mail_kind"]=="decision" for r in self.rows("mail"))==3)
        release.set()
        finished(self.studio,self.room)
        self.assertEqual([r["recipients"] for r in self.rows("mail") if r["mail_kind"]=="steer"], [["lead","dev"]]*4)
        self.assertEqual(sum(r["mail_kind"]=="decision" for r in self.rows("mail")),3)

    def test_unknown_target_never_becomes_broadcast_and_regular_consults_stop_at_ten(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def respond(agent,m,tools,opts):
            if agent == "room":
                text = m[-1]["content"]
                if text == "bad target":
                    return reply("",call("change_current_task",quote=text,recipients=[{}]))
                return self.steer(m)
            if agent == "lead":
                if m[-1]["content"].startswith("Decide as the task lead"):
                    return reply('{"action":"hold","text":"Recorded","to":[]}')
                return reply("Done") if tool_results(m) else reply("",call("delegate_task",assignee="dev",task="Check files."))
            entered.set()
            release.wait(8)
            return reply("Done")
        self.start(respond)
        self.assertTrue(entered.wait(5))
        self.assertEqual(self.studio.post(self.room,"bad target")["route"], "reply")
        self.assertEqual(self.rows("mail"), [])
        for n in range(11):
            self.studio.post(self.room,f"@lead update {n}")
        wait_for(lambda:sum(r["mail_kind"]=="decision" for r in self.rows("mail")) == 10)
        release.set()
        self.assertEqual(finished(self.studio,self.room)["status"],"completed")
        self.assertEqual(sum("update " in str(r["content"]) and "Message from the requester" in str(r["content"])
                             for r in self.provider.for_agent("lead")[-1]["messages"]),11)

    def test_pause_during_consult_preserves_unread_mail_without_applying_late_decision(self):
        member, consultation = threading.Event(),threading.Event()
        def respond(agent,m,tools,opts):
            if agent=="room":
                return self.steer(m)
            if agent=="lead":
                if m[-1]["content"].startswith("Decide as the task lead"):
                    consultation.set()
                    wait_for(opts["should_cancel"])
                    return reply('{"action":"forward","to":["dev"],"text":"stale decision"}')
                return reply("Delivered.") if tool_results(m) else reply("",call("delegate_task",assignee="dev",task="Check files."))
            if not member.is_set():
                member.set()
                wait_for(opts["should_cancel"])
                raise ProviderError("pausing",code="cancelled")
            return reply("Checked.")
        self.start(respond,stream=True)
        self.assertTrue(member.wait(5))
        self.studio.post(self.room,"@lead keep the file")
        self.assertTrue(consultation.wait(5))
        self.studio.pause_task(self.tid)
        wait_for(lambda:self.store.load_task(self.tid)["status"]=="paused")
        self.studio.resume_task(self.tid)
        self.assertEqual(finished(self.studio,self.room)["status"],"completed")
        self.assertEqual([e["agent"] for e in self.rows("mail_receipt")],["lead"])
        self.assertFalse(any("stale decision" in e["text"] for e in self.rows("mail")))
        self.assertTrue(any("keep the file" in str(row["content"]) for row in self.provider.for_agent("lead")[-1]["messages"]
                            if row["role"]=="user"))


class SilentStreamTest(unittest.TestCase):
    def test_idle_response_socket_is_woken_without_another_byte(self):
        from types import SimpleNamespace
        peer, sock = socket.socketpair()
        with peer, sock:
            response = SimpleNamespace(fp=SimpleNamespace(raw=SimpleNamespace(_sock=sock)))
            cancelled = threading.Event()
            timer = threading.Timer(.05,cancelled.set)
            timer.start()
            sock.settimeout(2)
            with _interruptible_reads(response,cancelled.is_set):
                self.assertEqual(sock.recv(1), b"")
            timer.join()


    def test_cancel_while_waiting_for_http_headers_closes_late_response_without_retry(self):
        import urllib.request
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from unittest.mock import patch
        from miniagent.provider_http import post_json

        entered, release, closed, cancelled = [threading.Event() for _ in range(4)]
        requests = []

        class Slow(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append(self.rfile.read(int(self.headers["Content-Length"])))
                entered.set()
                release.wait(5)
                self.send_response(200)
                self.send_header("Content-Length","2")
                self.end_headers()
                self.wfile.write(b"{}")
            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1",0),Slow)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(release.set)
        local = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        class Loopback:
            def open(self, req, timeout):
                redirected = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/",data=req.data)
                response = local.open(redirected,timeout=timeout)
                original = response.close
                def close():
                    original()
                    closed.set()
                response.close = close
                return response

        result = []
        def run():
            try:
                post_json("https://test.invalid/chat",{},api_key="test", timeout=10, should_cancel=cancelled.is_set)
            except ProviderError as exc:
                result.append(exc.code)
        with patch("miniagent.provider_http.urllib.request.build_opener", return_value=Loopback()):
            thread = threading.Thread(target=run,daemon=True)
            thread.start()
            self.assertTrue(entered.wait(3))
            cancelled.set()
            thread.join(1)
            self.assertEqual(result,["cancelled"])
            self.assertFalse(closed.is_set())
            release.set()
            self.assertTrue(closed.wait(2))
        self.assertEqual(len(requests),1)
