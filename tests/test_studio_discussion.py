"""Local UI actions use the waiting attempt, preserve full public evidence, and apply declared checks."""
import io
import json
import time
import unittest
import uuid
import zipfile
from unittest.mock import patch

from miniagent.providers import ScriptedProvider
from miniagent.studio_acceptance import build_checks
from tests import test_studio_chat_http as chat_http

tool = chat_http.tool


class DiscussionHTTPTest(unittest.TestCase):
    setUp = chat_http.StudioChatHTTPTest.setUp
    get = chat_http.StudioChatHTTPTest.get
    exchange = chat_http.StudioChatHTTPTest.exchange

    def start_chat(self, script, objective="prepare proposal", **options):
        factory = patch("miniagent.studio_service.DeepSeekProvider", return_value=ScriptedProvider(script))
        factory.start()
        self.addCleanup(factory.stop)
        status, data = self.exchange("POST", "/api/runs", {
            "request_id":str(uuid.uuid4()), "mode":"single", "objective":objective,
            "conversation":{"kind":"dm","members":["worker"],"recipient":"worker","intent":"edit"}, **options})
        self.assertEqual(status, 202, data)
        return data["run"]["id"]

    def detail_at(self, identity, statuses):
        end = time.monotonic() + 5
        while time.monotonic() < end:
            value = self.get("/api/runs/" + identity)
            if value["run"]["status"] in statuses:
                return value
            time.sleep(0.01)
        self.fail("did not reach expected state")

    def test_human_reply_stays_in_same_attempt_and_is_exportable(self):
        identity = self.start_chat([tool("ask_human", question="When is the money needed?"),
            tool("write_file", path="plan.json", content='{"portfolio":{"horizon":2}}'),
            {"text":"Use the two-year horizon."}], acceptance_checks=[
                {"kind":"json_equals","path":"plan.json","pointer":"/portfolio/horizon","expected":2}])
        base = "/api/runs/" + identity
        waiting = self.detail_at(identity, {"waiting_user"})
        attempt = waiting["tasks"][0]["current_attempt_id"]
        self.assertTrue(attempt)
        self.assertEqual(len(waiting["pending_questions"]), 1)
        q = waiting["pending_questions"][0]["request_id"]
        self.assertEqual(self.exchange("POST",base+"/questions/unknown",{"answer":"2"})[0],409)
        self.assertEqual(self.exchange("POST",base+"/questions/"+q,{"answer":"Two years."})[0],200)
        done = self.detail_at(identity, {"completed"})
        self.assertEqual(attempt,done["tasks"][0]["current_attempt_id"])
        self.assertFalse(done["pending_questions"])
        self.assertEqual(self.exchange("POST",base+"/questions/"+q,{"answer":"Two years."})[0],200)
        self.assertEqual(self.exchange("POST",base+"/questions/"+q,{"answer":"unrelated"})[0],409)
        self.assertEqual(self.exchange("POST",base+"/review",{"verdict":"passed","note":"Verified horizon and assumptions."})[0],200)
        from http.client import HTTPConnection
        conn = HTTPConnection("127.0.0.1",self.port,timeout=3)
        self.addCleanup(conn.close)
        conn.request("GET", base+"/room.zip")
        response = conn.getresponse()
        self.assertEqual(response.status,200)
        with zipfile.ZipFile(io.BytesIO(response.read())) as bundle:
            item = json.loads(bundle.read("conversation.json"))["turns"][0]
            self.assertEqual([r["kind"] for r in item["events"]], ["human_question","human_answer","human_review"])
            self.assertEqual(bundle.read("turn-001/artifacts/plan.json"), b'{"portfolio":{"horizon":2}}')
            self.assertTrue(json.loads(bundle.read("manifest.json"))[0]["files"][0]["sha256"])

    def test_read_full_public_reply_when_turn_preview_is_bounded(self):
        answer = "报告。\n" * 30_000 + "原文最后一句。"
        run = self.start_chat([{"text": answer}])
        turn = self.detail_at(run, {"unverified", "completed"})["conversation"]["turns"][-1]
        self.assertTrue(turn["answer_truncated"])
        self.assertNotIn("原文最后一句。", turn["answer"])
        self.assertEqual(self.get("/api/runs/" + run + "/room-answer"), {"answer": answer})

    def test_public_history_pages_do_not_clip_journal_or_user_text(self):
        run = self.start_chat([{"text": "Ready."}])
        self.detail_at(run, {"unverified", "completed"})
        base = "/api/runs/" + run
        self.assertEqual(self.get(base + "/room-answer"), {"answer":"Ready."})
        notes = [f"Review {i}" for i in range(26)] + ["完整意见" * 1000]
        for note in notes:
            self.assertEqual(self.exchange("POST", base + "/review", {"verdict":"passed", "note":note})[0], 200)
        latest = self.get(base + "/room-events")
        self.assertEqual(len(latest["events"]), 24)
        earlier = self.get(base + "/room-events?before=" + str(latest["start"]))
        self.assertEqual(earlier["start"], 0)
        self.assertEqual([e["text"] for e in earlier["events"] + latest["events"]], notes)
        self.assertEqual(self.get(base)["conversation"]["turns"][-1]["events"][-1]["text"], notes[-1])
        for query in ("before=NaN", "before=-1", "before=1&before=2", "other=1"):
            self.assertEqual(self.exchange("GET", base + "/room-events?" + query)[0], 400)

    def test_cancel_wakes_a_waiting_worker_and_rejects_late_reply(self):
        identity = self.start_chat([tool("ask_human", question="Which range?"), {"text":"unused"}])
        base = "/api/runs/"+identity
        q = self.detail_at(identity, {"waiting_user"})["pending_questions"][0]["request_id"]
        self.assertEqual(self.exchange("POST",base+"/cancel",{})[0],202)
        done = self.detail_at(identity, {"cancelled"})
        self.assertEqual(done["room_events"][-1]["kind"],"question_closed")
        self.assertEqual(self.exchange("POST",base+"/questions/"+q,{"answer":"later"})[0],409)

    def test_saved_profiles_available_for_new_rooms(self):
        profile = {"id":"lead","name":"技术负责人","prompt":"讨论需求并分工。"}
        for _ in range(2):
            self.assertEqual(self.exchange("POST","/api/profiles",profile)[0],200)
        self.assertEqual(self.get("/api/bootstrap")["custom_profiles"],[profile])
        self.assertEqual(self.exchange("POST","/api/profiles",{**profile,"name":"Another"})[0],409)
        self.assertEqual(self.exchange("POST","/api/profiles",{**profile,"id":"../lead"})[0],400)

    def test_absent_rule_data_or_wrong_value_does_not_pass(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as folder:
            root = Path(folder)
            checks=build_checks([], [{"kind":"json_equals","path":"stats.json","pointer":"/a~1b/0","expected":True}])
            self.assertFalse(checks(root)[0])
            for value, accepted in (("1",False),("true",True),('"true"',False)):
                (root/"stats.json").write_text('{"a/b":['+value+']}')
                self.assertEqual(checks(root)[0],accepted)
        with self.assertRaises(ValueError):
            build_checks([], [{"kind":"exec","path":"anything"}])
