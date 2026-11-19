"""The Studio assistant uses the same runs, role store and group conversations as the UI."""
import json
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.providers import ScriptedProvider
from miniagent.studio_service import StudioError, StudioService
from tests.support import cleanup, make_temp_dir
from tests.test_studio_conversations import RecordingProvider, msg, reply
from tests.test_studio_service import finished


def tool(tool_name, **arguments):
    return {"tool_calls": [{"name": tool_name, "arguments": arguments}]}


def message(text, parent=None):
    return {"request_id": str(uuid.uuid4()), "message": text, "parent_run_id": parent}


class StudioAssistantTest(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir("superagent")
        self.settings = Settings(api_key="test-key")
        self.service = StudioService(self.root, self.settings)
        self.management = {"create_studio_role", "create_studio_group", "list_studio_roles"}

    def tearDown(self):
        self.service.close()
        cleanup(self.root)

    def test_role_and_empty_group_are_real_linked_and_reusable_after_restart(self):
        role = dict(id="maintainer", name="维护员", prompt="负责处理电脑维护。")
        group = dict(name="电脑维护组", members=["reviewer", "maintainer"], lead="maintainer")
        scripts = [
            tool("create_studio_group", name="失败的尝试", members=["absent", "reviewer"], lead="absent"),
            tool("create_studio_role", **role), tool("create_studio_role", **role),
            tool("list_studio_roles"), tool("create_studio_group", **group), tool("create_studio_group", **group),
            tool("write_file", path="plan.txt", content="维护计划。"), {"text": "已创建维护员、维护组和计划。"}]
        provider = RecordingProvider(scripts)
        payload = message("创建维护员，与审阅者组建电脑维护组，给我一份简短计划")
        self.assertEqual(self.service.superagent_head(), {"conversation_id": None, "head_run_id": None})
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider):
            run = self.service.send_superagent(payload)
            detail = finished(self.service, run["id"])
        self.assertEqual(detail["run"]["status"], "unverified", detail["run"])
        self.assertTrue(detail["conversation"]["superagent"])
        self.assertEqual(len(detail["artifacts"]), 1)
        self.assertTrue(self.management <= set(provider.requests[0][1]))
        self.assertIn("Superagent", str(provider.requests[0][0]))
        entities = [e["entity"] for e in detail["conversation"]["turns"][0]["events"] if e["kind"] == "studio_entity"]
        self.assertEqual([e["kind"] for e in entities], ["role", "group"])
        room_id = entities[1]["id"]
        self.assertEqual(self.service.profiles(), [role])
        self.assertEqual(self.service.send_superagent(payload)["id"], run["id"])
        self.service.close()
        self.service = StudioService(self.root, self.settings)
        self.assertEqual(self.service.superagent_head()["head_run_id"], run["id"])
        empty = self.service.detail(room_id)
        self.assertEqual(empty["run"]["status"], "ready")
        self.assertEqual(empty["tasks"], [])
        self.assertEqual(empty["conversation"]["turns"], [])
        self.assertEqual(empty["conversation"]["title"], group["name"])
        self.assertEqual(empty["conversation"]["recipient"], "maintainer")
        self.assertTrue(empty["conversation"]["can_continue"])
        rows = self.service.list_conversations()["conversations"]
        self.assertEqual(next(row for row in rows if row["id"] == room_id)["turn_count"], 0)
        self.assertEqual(self.service.role_library("maintainer")["conversations"][0]["id"], room_id)
        self.assertNotIn(room_id, {r["id"] for r in self.service.list_runs()["runs"]})
        follow = RecordingProvider([{"text": "这是电脑维护组。"}])
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=follow):
            again = self.service.send_superagent(message("上次建了什么？", run["id"]))
            continued = finished(self.service, again["id"])["conversation"]
        self.assertIn("已创建维护员", json.dumps(follow.requests[0][0], ensure_ascii=False))
        self.assertEqual((continued["id"], len(continued["turns"])), (run["id"], 2))
        member = RecordingProvider([{"text": "先查看磁盘空间。"}])
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=member):
            first = self.service.start(msg("从哪里开始？", reply(room_id, "maintainer", "question")))
            native = finished(self.service, first["id"])["conversation"]
        self.assertTrue(self.management.isdisjoint(member.requests[0][1]))
        self.assertIn("负责处理电脑维护", json.dumps(member.requests[0][0], ensure_ascii=False))
        self.assertEqual((native["id"], native["head_run_id"], len(native["turns"])), (room_id, first["id"], 1))
        self.assertFalse(native["superagent"])
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=RecordingProvider([{"text": "已核对。"}])):
            review = self.service.start(msg("请核对。", reply(first["id"], "reviewer", "question")))
            updated = finished(self.service, review["id"])["conversation"]
        self.assertEqual((updated["title"], updated["recipient"]), (group["name"], "maintainer"))
        self.assertEqual(updated["turns"][-1]["recipient"], "reviewer")
        self.assertEqual(self.service.detail(room_id)["conversation"]["head_run_id"], review["id"])
        self.assertEqual(updated["lead"], "maintainer")
        wrong_lead = {**msg("由负责人安排检查", reply(review["id"], "reviewer")), "mode": "team"}
        with self.assertRaises(StudioError) as rejected:
            self.service.start(wrong_lead)
        self.assertEqual(rejected.exception.code, "invalid_group_lead")
        self.assertEqual(self.service.detail(room_id)["conversation"]["head_run_id"], review["id"])

    def test_stale_first_message_or_foreign_parent_cannot_fork_or_hijack(self):
        barrier = threading.Barrier(2)
        def submit():
            barrier.wait()
            try:
                return self.service.send_superagent(message("你好"))["id"]
            except StudioError as err:
                return err.code
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=ScriptedProvider([{"text": "你好"}])):
            with ThreadPoolExecutor(max_workers=2) as workers:
                results = list(workers.map(lambda _: submit(), range(2)))
            self.assertEqual(results.count("stale_head"), 1)
            finished(self.service, self.service.superagent_head()["head_run_id"])
        with self.assertRaisesRegex(StudioError, "统一对话已更新"):
            self.service.send_superagent(message("继续", "other-run"))
        self.assertEqual(len(self.service.list_conversations()["conversations"]), 1)

    def test_other_server_cannot_interrupt_a_live_turn_but_a_crashed_owner_is_resumable(self):
        started, release = threading.Event(), threading.Event()
        class Waiting(ScriptedProvider):
            def chat(self, messages, tools=None, temperature=0):
                started.set()
                release.wait(5)
                return super().chat(messages, tools, temperature)
        other = StudioService(self.root, self.settings)
        try:
            with patch("miniagent.studio_service.DeepSeekProvider", return_value=Waiting([{"text": "ok"}])):
                first = self.service.send_superagent(message("first"))["id"]
                self.assertTrue(started.wait(2))
                self.assertFalse(other.detail(first)["conversation"]["can_continue"])
                with self.assertRaises(StudioError) as error:
                    other.send_superagent(message("racing", first))
                self.assertEqual(error.exception.code, "stale_head")
                release.set()
                finished(self.service, first)
            # Snapshot of a published, pending turn after its owning process died.
            directory = self.root / "abandoned-turn"
            directory.mkdir()
            meta = self.service._meta(self.root / first)
            meta.update(state="running", title="interrupted", request_id="abandoned")
            meta["thread"].update(parent_run_id=first, ordinal=2)
            (directory / "studio.json").write_text(json.dumps(meta))
            head = other.detail(directory.name)["conversation"]
            self.assertTrue(head["can_continue"])
            self.assertEqual(head["turns"][-1]["status"], "interrupted")
            with patch("miniagent.studio_service.DeepSeekProvider", return_value=ScriptedProvider([{"text": "next"}])):
                fresh = other.send_superagent(message("new request", directory.name))
                self.assertEqual(len(finished(other, fresh["id"])["conversation"]["turns"]), 3)
        finally:
            release.set()
            other.close()

    def test_explicit_folder_switch_keeps_chat_and_gives_new_group_its_own_checkout(self):
        workspaces = make_temp_dir("assistant-workspaces")
        self.addCleanup(cleanup, workspaces)
        first_folder, next_folder = workspaces / "a", workspaces / "b"
        first_folder.mkdir()
        next_folder.mkdir()
        (first_folder / "only-a.txt").write_text("project A")
        (next_folder / "only-b.txt").write_text("project B")
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=RecordingProvider([
                tool("write_file", path="new-a.txt", content="A"), {"text": "Project A complete"}])):
            first = self.service.send_superagent({**message("处理项目 A"), "workspace_path": str(first_folder)})
            finished(self.service, first["id"])
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=RecordingProvider([
                tool("create_studio_group", name="B team", members=["reviewer","worker"], lead="reviewer"),
                tool("write_file", path="new-b.txt", content="B"), {"text": "B done"}])):
            second = self.service.send_superagent({**message("换到 B 并组群", first["id"]), "workspace_path": str(next_folder)})
            switched = finished(self.service, second["id"])
        self.assertEqual(switched["conversation"]["id"], first["id"])
        self.assertEqual(switched["conversation"]["workspace_path"], str(next_folder.resolve()))
        self.assertEqual(self.service._meta(self.root / second["id"])["thread"]["base_run_id"], None)
        self.assertFalse((self.root / second["id"] / "agents/worker/only-a.txt").exists())
        self.assertTrue((self.root / second["id"] / "agents/worker/only-b.txt").exists())
        self.assertTrue((next_folder / "new-b.txt").exists())
        self.assertFalse((next_folder / "new-a.txt").exists())
        group_id = switched["conversation"]["turns"][-1]["events"][0]["entity"]["id"]
        self.assertEqual(self.service.detail(group_id)["conversation"]["workspace_path"], str(next_folder.resolve()))
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=RecordingProvider([{"text": "ok"}])):
            third = self.service.send_superagent(message("继续 B", second["id"]))
            self.assertEqual(finished(self.service, third["id"])["run"]["workspace_path"], str(next_folder.resolve()))
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=RecordingProvider([{"text": "ok"}])):
            member = self.service.start(msg("首轮群消息", reply(group_id, "reviewer")))
            self.assertEqual(finished(self.service, member["id"])["run"]["workspace_path"], str(next_folder.resolve()))
        self.assertTrue((self.root / member["id"] / "agents/reviewer/new-b.txt").is_file())
