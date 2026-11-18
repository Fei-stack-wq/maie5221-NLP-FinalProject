"""Project turns preserve recipient, independent workspaces and execution history."""
from __future__ import annotations

import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.providers import ScriptedProvider
from miniagent.studio_service import StudioError, StudioService
from miniagent.studio_conversations import project_context
from tests.support import cleanup, make_temp_dir
from tests.test_studio_service import finished, request


def msg(text, conversation, **kwargs):
    return request("single", objective=text, conversation=conversation, **kwargs)


def fresh(recipient="worker", *, members=None, intent="edit"):
    members = members or [recipient]
    return {"kind": "group" if len(members) > 1 else "dm", "members": members,
            "intent": intent, "recipient": recipient}


def reply(run_id, recipient="worker", intent="edit"):
    return {"parent_run_id": run_id, "recipient": recipient, "intent": intent}


def tool(name, **arguments):
    return {"tool_calls": [{"name": name, "arguments": arguments}]}


class RecordingProvider(ScriptedProvider):
    def __init__(self, script):
        super().__init__(script)
        self.requests = []

    def chat(self, messages, tools=None, temperature=0):
        self.requests.append((messages.copy(), sorted(t.name for t in tools or [])))
        return super().chat(messages, tools, temperature)


class ConversationTest(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir("project-turns")
        self.runs = self.root / "runs"
        self.service = StudioService(self.runs, Settings(api_key="studio-test-sentinel"))

    def tearDown(self):
        self.service.close()
        cleanup(self.root)

    def run_turn(self, payload, script):
        provider = RecordingProvider(script)
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider) as factory:
            run = self.service.start(payload)
            detail = finished(self.service, run["id"])
        self.assertEqual(factory.call_count, 1)
        return detail, provider

    def seed(self, members=None):
        return self.run_turn(msg("设计一个游戏", fresh(members=members)), [
            tool("write_file", path="snake/game.js", content="const speed = 2; // initial version"),
            {"text": "初版使用速度 2。"},
        ])[0]["run"]["id"]

    def test_group_turns_pin_responder_and_publish_edits_separately_from_questions(self):
        first = self.seed(["worker", "reviewer", "writer"])
        question, provider = self.run_turn(msg("现在有多快？", reply(first, "reviewer", "question")), [
            tool("read_file", path="snake/game.js"),
            tool("write_file", path="unexpected.txt", content="must not exist"),
            {"text": "看过源码，速度为 2。"},
        ])
        second = question["run"]["id"]
        self.assertEqual(question["artifacts"], [])
        self.assertEqual(question["tasks"][0]["objective"], "现在有多快？")
        self.assertEqual(question["tasks"][0]["assignee_profile_id"], "reviewer")
        self.assertFalse(list((self.runs / second).rglob("unexpected.txt")))
        self.assertTrue(all(tools == ["find_files", "list_dir", "read_file", "read_lines", "search_files"] for _, tools in provider.requests))
        self.assertIn("初版使用速度 2。", json.dumps(provider.requests[0][0], ensure_ascii=False))
        self.assertEqual(question["conversation"]["artifact_run_id"], first)
        third, _ = self.run_turn(msg("改为速度 1", reply(second, "writer")), [
            tool("edit_file", path="snake/game.js", old_text="speed = 2", new_text="speed = 1"),
            {"text": "已调整为 1。"},
        ])
        head = third["run"]["id"]
        original = self.runs / first / "agents/worker/snake/game.js"
        self.assertIn("speed = 2", original.read_text())
        self.assertIn("speed = 1", (self.runs / head / "agents/writer/snake/game.js").read_text())
        self.service.close()
        self.service = StudioService(self.runs, Settings())
        room = self.service.detail(first)["conversation"]
        self.assertEqual((room["id"], room["head_run_id"], room["artifact_run_id"]), (first, head, head))
        self.assertEqual([turn["recipient"] for turn in room["turns"]], ["worker", "reviewer", "writer"])
        self.assertEqual([turn["answer"] for turn in room["turns"]], ["初版使用速度 2。", "看过源码，速度为 2。", "已调整为 1。"])

    def test_legacy_terminal_single_can_continue_without_a_saved_transcript(self):
        initial = self.service.start(request("demo"))
        parent = initial["id"]
        finished(self.service, parent)
        (self.runs / parent / "conversation-answer.txt").unlink()
        detail, provider = self.run_turn(msg("请解释之前的笔记", reply(parent, intent="question")), [
            tool("read_file", path="notes.md"), {"text": "笔记已读取。"},
        ])
        room = detail["conversation"]
        self.assertEqual(room["turns"][0]["answer"], "")
        self.assertEqual(room["artifact_run_id"], parent)
        self.assertIn("生成笔记", json.dumps(provider.requests[0][0], ensure_ascii=False))

    def test_fresh_question_and_first_edit_need_no_source_files(self):
        question, _ = self.run_turn(msg("从哪里开始？", fresh(intent="question")), [{"text": "先选配色。"}])
        self.assertIsNone(question["conversation"]["artifact_run_id"])
        result, _ = self.run_turn(msg("写一个调色板", reply(question["run"]["id"])), [
            tool("write_file", path="color.txt", content="blue"), {"text": "完成"},
        ])
        self.assertEqual(result["artifacts"][0]["name"], "color.txt")

    def test_stale_and_nonmember_requests_fail_before_model_and_do_not_create_run(self):
        root = self.seed(["worker", "reviewer"])
        with self.assertRaises(StudioError) as raised:
            self.service.start(msg("hello", reply(root, "researcher")))
        self.assertEqual(raised.exception.code, "non_member")
        payload = msg("check", reply(root, "reviewer", "question"))
        turn, _ = self.run_turn(payload, [{"text":"check complete"}])
        self.assertEqual(self.service.start(payload)["id"], turn["run"]["id"])
        with self.assertRaises(StudioError) as raised:
            self.service.start(msg("a different edit", reply(root)))
        self.assertEqual(raised.exception.code, "stale_head")
        self.assertEqual(len(self.service.list_runs()["runs"]), 2)

    def test_concurrent_process_handles_admit_only_one_successor(self):
        parent = self.seed()
        other = StudioService(self.runs, self.service.settings)
        barrier = threading.Barrier(2)
        def start(service):
            barrier.wait(timeout=5)
            try:
                run = service.start(msg("review", reply(parent, intent="question")))
                return run["id"]
            except StudioError as exc:
                return exc.code
        try:
            with patch("miniagent.studio_service.DeepSeekProvider", return_value=ScriptedProvider([{"text":"ok"}])):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(start, [self.service, other]))
                self.assertEqual(results.count("stale_head"), 1)
                self.service.close()
                other.close()
                room = self.service.detail(parent)["conversation"]
                self.assertEqual(len(room["turns"]), 2)
        finally:
            other.close()

    def test_failed_edit_retains_last_published_project_for_next_turn(self):
        parent = self.seed()
        failed, _ = self.run_turn(msg("edit", reply(parent), limits={"max_steps":1}), [
            tool("write_file", path="snake/game.js", content="partial change"),
        ])
        self.assertEqual(failed["run"]["status"], "failed")
        self.assertEqual(failed["conversation"]["artifact_run_id"], parent)
        question, _ = self.run_turn(msg("check", reply(failed["run"]["id"], intent="question")), [{"text":"checked"}])
        workspace = self.runs / question["run"]["id"] / "context/project/snake/game.js"
        self.assertIn("speed = 2", workspace.read_text())

    def test_unsupported_snapshot_error_redacts_sensitive_relative_names(self):
        parent = self.seed()
        workspace = self.runs / parent / "agents/worker"
        credential = "sk-" + "z" * 36
        for name in [credential + ".pdf", "studio-test-sentinel.pdf", "secret-input.pdf", "bad\nname.pdf"]:
            with self.subTest(name=name[:3]):
                path = workspace/name
                path.write_bytes(b"%PDF-1.4")
                try:
                    with self.assertRaises(StudioError) as rejected:
                        self.service.start(msg("Continue work.",reply(parent)))
                    message = str(rejected.exception)
                    self.assertEqual(rejected.exception.code,"unsupported_snapshot")
                    self.assertIn("文件名已隐藏",message)
                    self.assertNotIn(name,message)
                    self.assertNotIn(credential,message)
                    self.assertNotIn(str(workspace),message)
                finally:
                    path.unlink()

    def test_snapshot_error_does_not_publish_partial_run_or_call_provider(self):
        parent = self.seed()
        workspace = self.runs / parent / "agents/worker"
        secret = self.root / "outside.txt"
        secret.write_text("private outside file")
        bad = workspace / "readme.txt"
        bad.symlink_to(secret)
        with patch("miniagent.studio_service.DeepSeekProvider") as model:
            with self.assertRaises(StudioError) as raised:
                self.service.start(msg("edit", reply(parent)))
            self.assertEqual(raised.exception.code, "unsupported_snapshot")
            self.assertEqual(len(self.service.list_runs()["runs"]), 1)
            self.assertFalse(list(self.runs.glob(".studio-pending-*")))
            model.assert_not_called()
        bad.unlink()
        (workspace / "picture.png").write_bytes(b"binary")
        with self.assertRaises(StudioError):
            self.service.start(msg("edit", reply(parent)))

    def test_invalid_conversation_configs_and_foreign_threads_rejected(self):
        for invalid in (None, {}, {"kind":"group","members":["worker"],"intent":"edit","recipient":"worker"},
                        fresh(members=["worker", "worker"]), {**fresh(), "recipient":"researcher"},
                        {**fresh(), "kind":[]}, {**fresh(), "recipient":[]}, {**fresh(),"members":["worker",{}]},
                        {"parent_run_id":"../elsewhere","recipient":"worker","intent":"question"}):
            with self.subTest(invalid=invalid), self.assertRaises(StudioError):
                self.service.start(msg("hello",invalid))
        with self.assertRaises(StudioError):
            self.service.start(msg("hello", fresh(intent="question"), acceptance_files=["proof.txt"]))
        self.assertEqual(self.service.list_runs()["runs"], [])

    def test_public_answer_persisted_separately_and_key_redacted(self):
        answer = "答案" * 22000 + "studio-test-sentinel"
        detail, _ = self.run_turn(msg("answer", fresh(intent="question")), [{"text":answer}])
        public_answer = detail["conversation"]["turns"][0]["answer"]
        self.assertTrue(public_answer.endswith("[REDACTED]"))
        self.assertNotIn("studio-test-sentinel", public_answer)
        metadata = self.runs / detail["run"]["id"] / "studio.json"
        self.assertLess(metadata.stat().st_size, 8192)


    def test_model_context_retains_goal_and_latest_turn_after_many_long_replies(self):
        history = [{"user": f"消息 {i}", "answer": "答" * 100000, "recipient": "worker",
                    "status": "unverified", "answer_truncated": False} for i in range(15)]
        context = project_context(fresh(), history)
        self.assertIn("项目的初始要求：消息 0", context)
        self.assertIn("消息 14", context)
        self.assertIn("省略了较早的", context)
        self.assertLess(len(context.encode()), 100 * 1024)
        self.assertTrue(all(len(turn["answer"]) == 100000 for turn in history))

    def test_same_role_in_two_rooms_cannot_see_unrelated_transcript_or_files(self):
        first, _ = self.run_turn(msg("private-room-sentinel", fresh()), [
            tool("write_file", path="private.txt", content="only room one"),
            {"text":"unrelated-reply-sentinel"},
        ])
        second, provider = self.run_turn(msg("另一个房间", fresh()), [{"text":"这里是第二个房间"}])
        self.assertNotEqual(first["conversation"]["id"], second["conversation"]["id"])
        self.assertEqual(len(second["conversation"]["turns"]), 1)
        messages = json.dumps(provider.requests[0][0], ensure_ascii=False)
        self.assertNotIn("private-room-sentinel", messages)
        self.assertNotIn("unrelated-reply-sentinel", messages)
        self.assertEqual(second["artifacts"], [])
