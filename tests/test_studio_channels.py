"""Local sidebar views group immutable turn history by its real conversation identity."""
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.providers import ScriptedProvider
from miniagent.studio_service import StudioError, StudioService
from tests.test_studio_conversations import fresh, msg, reply
from tests.test_studio_service import finished


class ChannelsTest(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.settings = Settings(api_key="directory-fixture-key")
        self.runs = self.root / "runs"
        self.service = self.server()

    def server(self):
        service = StudioService(self.runs, self.settings)
        self.addCleanup(service.close)
        return service

    def channel(self, name="电脑维护", **extra):
        return {"request_id": str(uuid.uuid4()), "members": ["reviewer", "worker"],
                "name": name, "lead": "reviewer", **extra}

    def run_turn(self, objective, conversation):
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=ScriptedProvider([{"text": "收到"}])):
            run = self.service.start(msg(objective, conversation))
            return finished(self.service, run["id"])

    def test_named_rooms_are_open_before_the_first_message_and_keep_their_original_lead(self):
        payload = self.channel()
        row = self.service.create_channel(payload)
        identity = row["id"]
        self.assertEqual((row["head_run_id"], row["turn_count"], row["pinned"], row["last_message"]),
                         (identity, 0, True, ""))
        self.assertEqual(row["member_names"], ["审阅者", "通用助手"])
        self.assertEqual(self.service.list_runs()["runs"], [])
        empty = self.service.detail(identity)["conversation"]
        self.assertTrue(empty["can_continue"])
        self.assertEqual(empty["turns"], [])
        sent = self.run_turn("请核对本周的保养记录", reply(identity, "worker", "question"))
        self.assertEqual(sent["conversation"]["lead"], "reviewer")
        self.assertEqual(sent["conversation"]["turns"][0]["recipient"], "worker")
        self.assertEqual(sent["conversation"]["id"], identity)
        expected = (sent["run"]["id"], 1, "请核对本周的保养记录")
        again = self.server()
        listed = again.directory_chats()
        self.assertEqual(listed["recent"], listed["channels"])
        row = again.create_channel(payload)
        self.assertEqual((row["head_run_id"], row["turn_count"], row["last_message"]), expected)
        self.assertEqual(again.update_channel(identity, {"pinned": False})["title"], "电脑维护")
        self.assertEqual(self.service.directory_chats()["channels"], [])
        self.assertEqual(self.service.detail(identity)["conversation"]["head_run_id"], expected[0])

    def test_pinned_old_groups_remain_distinct_even_with_identical_names_and_members(self):
        members = ["reporter", "reviewer"]
        fresh_group = {**fresh("reporter", members=members, intent="question"),
                       "profiles": [{"id": "reporter", "name": "记者", "prompt": "查资料。"}]}
        first = self.run_turn("对照原始文件", fresh_group)["run"]["id"]
        second = self.run_turn("对照原始文件", fresh_group)["run"]["id"]
        dm = self.run_turn("私聊", fresh("reviewer", intent="question"))["run"]["id"]
        latest = self.run_turn("补查发布日期", reply(first, "reviewer", "question"))["run"]["id"]
        with patch.object(self.service, "_events", side_effect=AssertionError("directory scanned traces")):
            listing = self.service.directory_chats()
            self.assertEqual(listing["channels"], [])
            self.assertEqual([row["id"] for row in listing["recent"]], [first, dm, second])
            self.assertEqual(listing["recent"][0]["head_run_id"], latest)
            self.assertEqual(listing["recent"][0]["last_message"], "补查发布日期")
            self.assertEqual(listing["recent"][0]["member_names"], ["记者", "审阅者"])
        root_meta = (self.runs / first / "studio.json").read_bytes()
        row = self.service.update_channel(first, {"pinned": True, "name": "校报编辑部"})
        self.assertEqual(row["head_run_id"], latest)
        self.assertEqual(self.server().directory_chats()["channels"], [row])
        self.assertEqual(self.service.detail(latest)["conversation"]["title"], "校报编辑部")
        self.assertEqual((self.runs / first / "studio.json").read_bytes(), root_meta)
        row = self.service.update_channel(first, {"pinned": False})
        self.assertEqual((row["pinned"], row["title"]), (False, "校报编辑部"))
        self.assertEqual(self.service.directory_chats()["channels"], [])
        self.assertEqual(self.service.update_channel(first, {"pinned": True})["title"], "校报编辑部")
        for invalid in (latest, dm):
            with self.assertRaisesRegex(StudioError, "已有群聊"):
                self.service.update_channel(invalid, {"pinned": True})
        with self.assertRaises(StudioError):
            self.service.update_channel(first, {"pinned": "false"})
        self.assertEqual(len(self.service.directory_chats()["recent"]), 3)
        self.assertEqual(self.service.update_channel(second, {"pinned": True})["title"], "记者、审阅者的群聊")

    def test_retries_across_servers_converge_and_bad_input_leaves_no_room(self):
        payload = self.channel()
        other = self.server()
        barrier = threading.Barrier(2)
        def submit(service):
            barrier.wait()
            return service.create_channel(payload)["id"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(submit, (self.service, other)))
        self.assertEqual(len(set(ids)), 1)
        for changes in ({"name": "换名"}, {"members": ["writer", "reviewer"]}, {"lead": "worker"}):
            with self.assertRaises(StudioError) as error:
                other.create_channel({**payload, **changes})
            self.assertEqual(error.exception.code, "group_conflict")
        for changes in ({"name": "  "}, {"lead": "missing"}, {"request_id": 17}, {"members": ["worker", "worker"]},
                        {"name": "两行\n文本"}, {"workspace_path": str(self.runs)}):
            with self.assertRaises(ValueError):
                self.service.create_channel({**self.channel(), **changes})
        self.assertEqual(len(other.directory_chats()["recent"]), 1)
        symlink = self.runs / ids[0] / "studio-channel.json"
        private = self.root / "private.txt"
        private.write_text('{"pinned":false,"name":"private"}', encoding="utf-8")
        symlink.symlink_to(private)
        self.assertEqual(self.service.directory_chats()["recent"][0]["title"], "电脑维护")
        with self.assertRaises(StudioError):
            self.service.update_channel(ids[0], {"pinned": False, "name": "别名"})
        self.assertNotIn("别名", private.read_text())

    def test_sidebar_pins_do_not_grant_cross_project_delegation_or_access_to_old_groups(self):
        workspace = self.root / "office"
        workspace.mkdir()
        row = self.service.create_channel(self.channel(workspace_path=str(workspace)))
        no_folder = self.service.create_channel(self.channel())
        source = {"thread": {"id": "assistant", "superagent": True}, "workspace_path": str(workspace)}
        other = {**source, "workspace_path": str(self.root / "other")}
        self.service.update_channel(row["id"], {"name": "维护频道", "pinned": False})
        self.assertEqual(self.service.studio_routing_group(source, row["id"])["name"], "维护频道")
        self.assertIsNone(self.service.studio_routing_group(other, row["id"]))
        self.assertIsNone(self.service.studio_routing_group({"thread": source["thread"]}, no_folder["id"]))
        legacy = self.run_turn("旧群", fresh(members=["worker", "reviewer"], intent="question"))["run"]["id"]
        self.service.update_channel(legacy, {"pinned": True})
        self.assertIsNone(self.service.studio_routing_group(source, legacy))
