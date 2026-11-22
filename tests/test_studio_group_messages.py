"""A group conversation outlives task attempts, and reading it cannot dispatch a worker."""
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.studio_service import StudioError, StudioService
from tests.test_room_reply import output
from tests.test_studio_conversations import RecordingProvider, tool
from tests.test_studio_service import finished
from tests.test_studio_steering import PausedModel


class GroupMessagesTests(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(tmp) / "runs"
        self.local = Path(tmp) / "workspace"
        self.local.mkdir()
        self.settings = Settings(api_key="fixture")
        self.svc = StudioService(self.root, self.settings)
        self.addCleanup(self.svc.close)
        self.room = self.svc.create_channel({"request_id": str(uuid.uuid4()), "name": "Device team",
            "lead": "worker", "members": ["worker"], "workspace_path": str(self.local)})["id"]

    def send(self, text, parent=None, **fields):
        return self.svc.send_group_message(self.room, {"request_id": str(uuid.uuid4()),
                "parent_run_id": parent or self.room, "text": text, **fields})

    def test_room_can_converse_before_work_then_recall_it_after_restart_without_new_task(self):
        worker = RecordingProvider([tool("write_file", path="release.txt", content="a real delivery"), {"text":"Published."}])
        with patch("miniagent.studio_room_reply.DeepSeekProvider.chat", side_effect=[
                output(answer="我们选择代号橙花。"), output("work", quote="按刚才的决定写交付说明"),
                output(answer="我们交付了 release.txt。")]) as router, patch("miniagent.studio_service.DeepSeekProvider", return_value=worker) as jobs:
            first = self.send("我们之前商量的代号是什么")
            self.assertEqual(first["message"]["disposition"], "reply")
            self.assertEqual(len(self.svc.detail(self.room)["conversation"]["turns"]), 0)
            self.assertFalse(list(self.root.glob("*/tasks.sqlite3")))
            run = self.send("按刚才的决定写交付说明")["run"]["id"]
            self.assertEqual(finished(self.svc, run)["run"]["status"], "unverified")
            self.assertIn("橙花", str(worker.requests[0][0]))
            before = list(self.root.glob("*/tasks.sqlite3"))
            self.assertEqual(len(before), 1)
            self.svc.close()
            self.svc = StudioService(self.root, self.settings)
            self.addCleanup(self.svc.close)
            last = self.send("你都做了啥", run)
            self.assertEqual(last["message"]["text"], "我们交付了 release.txt。")
            self.assertEqual(last["message"]["model_receipt"]["model"], "test-model")
            self.assertIn("release.txt", str(router.call_args.args))
            self.assertEqual(jobs.call_count, 1)
            self.assertEqual(len(self.svc.detail(run)["conversation"]["messages"]), 3)
            self.assertEqual(list(self.root.glob("*/tasks.sqlite3")), before)
            self.assertEqual((self.local / "release.txt").read_text(), "a real delivery")

    def test_concurrent_retries_answer_once_and_reused_id_requires_the_same_content(self):
        entered, release = threading.Event(), threading.Event()
        def pause(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return output(answer="收到。")
        p = {"request_id": str(uuid.uuid4()), "parent_run_id": self.room, "text": "聊聊这个。"}
        with patch("miniagent.studio_room_reply.DeepSeekProvider.chat", side_effect=pause) as model, ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.svc.send_group_message, self.room, p)
            try:
                self.assertTrue(entered.wait(5))
                second = pool.submit(self.svc.send_group_message, self.room, p)
            finally:
                release.set()
            self.assertEqual(first.result(), second.result())
            self.assertEqual(model.call_count, 1)
            with self.assertRaises(StudioError) as conflict:
                self.svc.send_group_message(self.room, {**p, "text": "做另外的事"})
            self.assertEqual(conflict.exception.code, "conflict")
        self.assertFalse(list(self.root.glob("*/tasks.sqlite3")))

    def test_message_thinking_is_scoped_to_the_question_and_queued_work(self):
        with patch("miniagent.studio_room_reply.DeepSeekProvider") as router, patch(
                "miniagent.studio_service.DeepSeekProvider", return_value=RecordingProvider([{"text": "已检查"}])) as worker:
            with self.assertRaisesRegex(StudioError, "thinking"):
                self.send("情况如何？", options={"thinking": True})
            router.return_value.chat.side_effect = [
                output(answer="已收到。"), output(answer="就绪。"), output("work", quote="请检查文件")]
            for choice, expected in [({"thinking": "disabled"}, "disabled"), ({}, "enabled")]:
                row = self.send("情况如何？", intent="question", options=choice)["message"]
                self.assertEqual(row["model_receipt"]["thinking"], expected)
                self.assertEqual(router.call_args.kwargs["thinking"], expected)
            run = self.send("请检查文件", options={"thinking": "disabled"})["run"]
            finished(self.svc, run["id"])
            self.assertEqual(router.call_args.kwargs["thinking"], "disabled")
            self.assertEqual(worker.call_args.kwargs["thinking"], "disabled")

    def test_existing_work_options_do_not_turn_a_question_into_an_order(self):
        with patch("miniagent.studio_room_reply.DeepSeekProvider.chat",
                   return_value=output(answer="报告应当记录测试结果。")):
            row = self.send("为什么要准备这一份报告？", options={
                "acceptance_files":["QA.md"], "conversation":{"recover_failed": True}})["message"]
        self.assertEqual(row["disposition"], "reply")
        self.assertNotIn("linked_run_id", row)
        self.assertFalse(list(self.root.glob("*/tasks.sqlite3")))

    def test_reply_retains_evidence_and_follows_a_release_that_arrived_while_answering(self):
        worker = PausedModel([{"text":"OK"}], pause_at=0)
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=worker):
            run = self.send("交付这个版本", intent="work")["run"]["id"]
            refs = [{"run_id": run, "path": "README.md", "ref": "agents/worker/README.md"}]
            def reply(*args, **kwargs):
                self.assertTrue(kwargs["active"])
                worker.gate.release.set()
                finished(self.svc, run)
                return {"action":"reply", "text":"见 README.md。", "cited_artifacts": refs}
            try:
                self.assertTrue(worker.gate.entered.wait(5))
                with patch("miniagent.studio_room_reply.decide_room_message", side_effect=reply):
                    row = self.send("刚才提到哪个文件", run)["message"]
            finally:
                worker.gate.release.set()
        self.assertTrue(row["after_turn"])
        self.assertEqual(row["cited_artifacts"], refs)
        self.svc.close()
        self.svc = StudioService(self.root, self.settings)
        self.addCleanup(self.svc.close)
        restored = self.svc.detail(run)["conversation"]["messages"][-1]
        self.assertEqual(restored, row)

    def test_question_during_work_is_not_steering_but_explicit_change_reaches_same_task_with_context(self):
        worker = PausedModel([{"text":"Will resume."}, {"text":"Acknowledged."}], pause_at=0)
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=worker), patch(
                "miniagent.studio_room_reply.DeepSeekProvider.chat", side_effect=[
                    output(answer="目前按甲方案执行。"), output("steer", quote="改用乙方案")]):
            run = self.send("先做这项工作", intent="work")["run"]["id"]
            try:
                self.assertTrue(worker.gate.entered.wait(5))
                read = self.send("你刚才说的是什么方案？", run)
                self.assertEqual(read["message"]["disposition"], "reply")
                self.assertEqual(self.svc._steering[run].rows, [])
                change = self.send("好，改用乙方案", run)
                self.assertEqual(change["message"]["linked_run_id"], run)
                self.assertEqual(change["message"]["disposition"], "steer")
                self.assertEqual(len(self.svc._steering[run].rows), 1)
            finally:
                worker.gate.release.set()
            detail = finished(self.svc, run)
            self.assertIn("改用乙方案", str(worker.requests[-1][0]))
            self.assertIn("目前按甲方案执行", str(worker.requests[-1][0]))
            self.assertEqual(len(detail["conversation"]["turns"]), 1)
