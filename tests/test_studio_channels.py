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

    def test_membership_changed_during_execution_applies_to_next_task_and_keeps_old_attribution(self):
        workspace = self.root / "project"
        workspace.mkdir()
        room = self.service.create_channel(self.channel(workspace_path=str(workspace)))["id"]
        self.service.add_profile({"id": "editor", "name": "主编", "prompt": "编辑并校对。"})
        entered, release = threading.Event(), threading.Event()
        class WaitingProvider(ScriptedProvider):
            def chat(self, messages, tools=None, temperature=0):
                entered.set()
                if not release.wait(5):
                    raise AssertionError("test did not release the model")
                return super().chat(messages, tools, temperature)
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=WaitingProvider([{"text": "收到"}])):
            live = self.service.start(msg("查看今天的记录", reply(room, "worker", "question")))["id"]
            try:
                self.assertTrue(entered.wait(3))
                other = self.server()
                roster = other.update_members(room, {"members": ["editor", "writer"], "lead": "editor", "revision": 0})
                self.assertEqual(roster, {"conversation_id": room, "revision": 1, "lead": "editor",
                                          "members": ["editor", "writer"], "member_names": {"editor": "主编", "writer": "写作者"}})
                self.assertEqual(other._meta(self.runs / live)["thread"]["members"], ["reviewer", "worker"])
                self.assertFalse(other.detail(live)["conversation"]["can_continue"])
            finally:
                release.set()
            history = finished(self.service, live)["conversation"]
        self.assertEqual((history["members"], history["lead"], history["membership_revision"]),
                         (["editor", "writer"], "editor", 1))
        last = history["turns"][-1]
        self.assertEqual((last["recipient"], last["member_names"]["worker"], last["role_keys"]["worker"]),
                         ("worker", "通用助手", "worker"))
        removed = self.service.role_directory("worker")
        self.assertEqual(removed["conversations"], [])
        self.assertEqual([r["run_id"] for r in removed["tasks"]], [live])
        self.assertEqual([c["id"] for c in self.service.role_directory("editor")["conversations"]], [room])
        self.assertEqual(self.service.role_library("editor")["conversations"][0]["id"], room)
        with self.assertRaises(StudioError) as error:
            self.service.start(msg("请回复", reply(live, "worker")))
        self.assertEqual(error.exception.code, "non_member")
        sent = self.run_turn("继续校对", reply(live, "editor"))
        self.assertEqual(sent["conversation"]["turns"][-1]["recipient"], "editor")
        source = {"thread": {"superagent": True}, "workspace_path": str(workspace)}
        self.assertEqual(self.service.studio_routing_group(source, room)["lead"], "editor")
        self.assertEqual(other.list_conversations()["conversations"][0]["members"], ["editor", "writer"])

    def test_compare_and_swap_across_servers_and_queued_participant_removal(self):
        room = self.service.create_channel(self.channel())["id"]
        services = [self.service, self.server()]
        gate = threading.Barrier(2)
        def change(pair):
            service, member = pair
            gate.wait()
            try:
                return service.update_members(room, {"revision": 0, "members": [member], "lead": member})["lead"]
            except StudioError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(change, zip(services, ["writer", "researcher"])))
        self.assertEqual(results.count("stale_membership"), 1)
        roster = self.server().channel_members(room)
        self.assertEqual(roster["revision"], 1)
        removed = "writer" if roster["lead"] == "researcher" else "researcher"
        with self.assertRaises(StudioError) as exc:
            self.service.start(msg("已排队的研究分工", reply(room, roster["lead"])),
                               routed={"child_run_id": "unused", "group_id": room, "participants": [removed]})
        self.assertEqual(exc.exception.code, "missing_members")
        self.assertFalse((self.runs / "unused").exists())

    def test_empty_and_single_member_channels_can_invite_remove_everyone_and_reopen(self):
        # One action may create the conversation together with its first message.
        now = self.run_turn("立即回复", {"kind": "group", "members": ["worker"], "intent": "question",
                                     "recipient": "worker"})
        self.assertEqual((now["conversation"]["kind"], now["conversation"]["members"]), ("group", ["worker"]))
        empty = self.channel(members=[], lead=None)
        room = self.service.create_channel(empty)["id"]
        self.assertIsNone(self.service.channel_members(room)["lead"])
        self.assertFalse(self.service.detail(room)["conversation"]["can_continue"])
        with self.assertRaises(StudioError) as error:
            self.service.start(msg("发送", reply(room)))
        self.assertEqual(error.exception.code, "empty_group")
        self.assertEqual(self.service.create_channel(self.channel(members=["reviewer"], lead="reviewer"))["members"],
                         ["reviewer"])
        for bad in ({"members": ["unknown"], "lead": "unknown"}, {"members": [], "lead": "worker"},
                    {"members": ["writer"], "lead": "worker"}, {"members": ["writer", "WRITER"], "lead": "writer"}):
            with self.assertRaises(StudioError):
                self.service.update_members(room, {"revision": 0, **bad})
        self.service.update_members(room, {"revision": 0, "members": ["worker"], "lead": "worker"})
        self.assertEqual(self.service.create_channel(empty)["id"], room)
        first = self.run_turn("群内独立回答", reply(room, "worker", "question"))
        self.assertEqual(first["conversation"]["kind"], "group")
        self.service.update_members(room, {"revision": 1, "members": [], "lead": None})
        empty_room = self.server().detail(first["run"]["id"])["conversation"]
        self.assertEqual((empty_room["members"], empty_room["lead"], empty_room["can_continue"]), ([], None, False))
        self.assertEqual(empty_room["turns"][0]["recipient"], "worker")

    def test_historical_local_profiles_and_lead_survive_membership_updates(self):
        profile = {"id": "reporter", "name": "记者", "prompt": "查资料。"}
        original = {**fresh("reporter", members=["reporter", "reviewer"], intent="question"), "profiles": [profile]}
        room = self.run_turn("旧群任务", original)["run"]["id"]
        # A subsequently saved homonym must not silently replace a historical identity.
        self.service.add_profile({**profile, "name": "同名编辑", "prompt": "编辑。"})
        updated = self.service.update_members(room, {"revision": 0, "members": ["reporter", "writer"], "lead": "writer"})
        self.assertEqual(updated["member_names"]["reporter"], "记者")
        history = self.run_turn("查原文", reply(room, "reporter", "question"))["conversation"]
        self.assertEqual(history["profiles"], [profile])
        self.assertNotIn("reporter", history["saved_roles"])
        self.assertEqual(history["lead"], "writer")
