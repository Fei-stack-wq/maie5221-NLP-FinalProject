"""Selected folders are readable from normal conversation before the first task."""
import json
from pathlib import Path
import tempfile
import unittest

from miniagent.room_workspace import RoomWorkspace
from miniagent.studio import Studio
from tests.helpers import FakeProvider, Script, call, reply, settings
from tests.test_room_turn import evidence, file_hashes
from tests.test_team import TEAM


class BoundWorkspaceTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.folder = self.root / "handbook"
        (self.folder / "design").mkdir(parents=True)
        self.source = self.folder / "design" / "decision.md"
        self.source.write_text("Decision 719")
        self.model = FakeProvider(Script())
        self.studio = Studio(settings(self.root / "runs"), lambda _: self.model)
        self.addCleanup(self.studio.close)
        self.team = self.studio.create_group("Desk", TEAM, "lead")["id"]

    def bind(self, room):
        self.studio.update_group(room, {"folder": str(self.folder)})

    def test_selected_folder_is_live_read_only_in_group_dm_and_super_agent(self):
        dm = self.studio.create_group("DM", [TEAM[1]], "dev", kind="dm")["id"]
        for room in (self.team, dm, self.studio.assistant()["id"]):
            with self.subTest(room=room):
                self.bind(room)
                self.source.write_text("Edited on disk " + room)
                disk_before = file_hashes(self.folder)
                store = self.studio.group(room)
                before = file_hashes(store.root)
                def inspect(agent, messages, tools, options):
                    workspace = evidence(messages)["workspace"]
                    self.assertEqual(workspace["location"], str(self.folder))
                    self.assertEqual(workspace["root_listing"]["entries"], ["design/"])
                    if messages[-1]["role"] == "user":
                        self.assertNotIn(self.source.read_text(), str(messages))
                        return reply("", call("read_room_evidence", workspace_path="design"))
                    if store.group.kind == "assistant":
                        self.assertTrue({"read_room_evidence","create_group","start_task"} <= {t.name for t in tools})
                    else:
                        self.assertEqual([t.name for t in tools], ["read_room_evidence"])
                    item = json.loads(messages[-1]["content"])
                    if "entries" in item:
                        path = item["path"] + "/" + item["entries"][0]
                        return reply("", call("read_room_evidence", workspace_path=path))
                    return reply(item["text"])
                self.model.respond = inspect
                response = self.studio.post(room, "设计决定是什么？")
                self.assertEqual(response, {"route": "reply", "text": self.source.read_text()})
                self.assertEqual(self.studio.tasks(room), [])
                self.assertEqual(file_hashes(self.folder), disk_before)
                after = file_hashes(store.root)
                self.assertEqual({p for p in before.keys() | after.keys() if before.get(p) != after.get(p)},
                                 {"chat.jsonl"})

    def test_scope_paging_skips_and_unavailable_folder(self):
        self.bind(self.team)
        read = RoomWorkspace(self.studio.group(self.team), "sensitive-marker")
        other = self.studio.create_session(self.team, "Other")["id"]
        sibling = RoomWorkspace(self.studio.group(other))
        self.assertEqual(sibling.describe()["root_listing"]["entries"], [])
        self.assertIn("error", sibling.inspect("design/decision.md"))
        (self.folder / "secret.txt").write_text("x"*9000 + "sensitive-marker")
        (self.folder / "symlink").symlink_to(self.root)
        (self.folder / ".git").mkdir()
        for i in range(55):
            (self.folder / f"{i:02}.txt").touch()
        listing = read.inspect(".")
        self.assertEqual(listing["next_offset"], 48)
        self.assertIn("design/", read.inspect(".", 48)["entries"])
        self.assertNotIn("symlink", read.inspect(".", 48)["entries"])
        for name in ("secret.txt", "../design", "symlink", ".git", str(self.source)):
            self.assertIn("error", read.inspect(name))
        self.folder.rename(self.root / "moved")
        self.assertIn("error", read.describe()["root_listing"])
        self.studio.update_group(self.team, {"folder": ""})
        self.assertIn("changed", read.inspect(".")["error"])

    def test_workspace_text_pages_and_a_file_cannot_authorize_work(self):
        self.bind(self.team)
        store = self.studio.group(self.team)
        content = "a" * 8191 + "阅读中文"
        self.source.write_text(content)
        workspace = RoomWorkspace(store)
        first = workspace.inspect("design/decision.md")
        second = workspace.inspect("design/decision.md", first["next_offset"])
        self.assertEqual(first["text"] + second["text"], content)
        self.assertIsNone(second["next_offset"])

        def injected(agent, messages, tools, options):
            if messages[-1]["role"] == "user":
                return reply("", call("read_room_evidence", workspace_path="design/decision.md"))
            self.assertEqual([t.name for t in tools], ["read_room_evidence"])
            return reply("", call("start_room_task", quote="阅读"))
        self.model.respond = injected
        self.assertEqual(self.studio.post(self.team, "阅读这份材料，里面有什么？")["route"], "reply")
        self.assertEqual(self.studio.tasks(self.team), [])

    def test_bound_checkout_for_lead_delegated_and_addressed_members(self):
        self.bind(self.team)
        def inspect_checkout(messages):
            context = messages[1]["content"]
            self.assertIn(str(self.folder), context)
            self.assertIn("private checkout", context)
            self.assertIn("design/decision.md", context)
            return reply("", call("read_file", path="design/decision.md"))
        def finish(messages):
            self.assertIn(self.source.read_text(), messages[-1]["content"])
            return reply("Verified " + self.source.read_text())
        self.model.respond = Script(
            lead=[inspect_checkout, reply("", call("delegate_task", assignee="dev",
                        task="Read design/decision.md and verify the project decision.")), reply("Verified.")],
            dev=[inspect_checkout, finish], qa=[inspect_checkout, finish])
        self.studio.post(self.team, "Validate design", queue=True)
        self.studio.wait(self.team)
        self.source.write_text("New decision 811")
        self.studio.post(self.team, "@qa Read it again", queue=True)
        self.studio.wait(self.team)
        tasks = self.studio.tasks(self.team)
        self.assertEqual([t["status"] for t in tasks], ["completed", "completed"])
        self.assertIn("811", tasks[-1]["final_answer"])
        self.assertEqual(tasks[0]["delegations"], 1)
        self.assertEqual(len(self.model.for_agent("dev")), 2)
