"""The role directory must describe actual assignments without adopting historical identities."""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.studio_service import StudioService
from miniagent.task_service import TaskService
from miniagent.task_store import TaskStore
from tests import test_studio_chat_http as chat_http


class RoleDirectoryTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.studio = StudioService(Path(temp.name), Settings())
        self.addCleanup(self.studio.close)
        self.dev = {"id": "dev", "name": "Developer", "prompt": "Write the requested features."}

    def record(self, run, *, kind="group", members=("writer", "dev", "reviewer"),
               recipient="writer", profiles=None, saved=(), mode="team", parent=None,
               root=None, ordinal=1, superagent=False, assignments=()):
        folder = self.studio.runs_dir / run
        folder.mkdir()
        thread = {"id": root or run, "kind": kind, "members": list(members), "recipient": recipient,
                  "profiles": profiles if profiles is not None else [self.dev], "intent": "edit",
                  "ordinal": ordinal, "parent_run_id": parent, "saved_roles": list(saved)}
        if superagent:
            thread["superagent"] = True
        meta = {"title": run + " work", "state": "completed", "mode": mode,
                "thread": thread, "created_at": "2026-10-17T14:01:00.000Z"}
        (folder / "studio.json").write_text(json.dumps(meta))
        service = TaskService(TaskStore(folder / "tasks.sqlite3"))
        if mode != "room":
            service.create(objective="Coordinate" if kind == "group" else "Personal task",
                           creation_key="root", task_id=run, owner=recipient, assignee=recipient)
        for i, member in enumerate(assignments):
            service.create(objective="Assignment " + str(i), creation_key=str(i), task_id=f"{run}-{i}",
                           parent_id=run, owner=recipient, assignee=member)
        return thread

    def keys(self, identity):
        return {role["source"]: role["key"] for role in self.studio.role_directory()["roles"] if role["id"] == identity}

    def test_separates_group_membership_from_assignment_and_links_the_exact_turn(self):
        self.record("initial", assignments=["dev", "dev"])
        self.record("second", parent="initial", root="initial", recipient="reviewer", ordinal=2, mode="single")
        self.record("dm", kind="dm", members=["dev"], recipient="dev", mode="single")
        self.record("empty", ordinal=0, mode="room")
        dev_key = self.keys("dev")["historical"]
        detail = self.studio.role_directory(dev_key)
        self.assertEqual(detail["role"]["conversation_count"], 3)
        self.assertEqual({room["id"] for room in detail["conversations"]}, {"dm", "empty", "initial"})
        self.assertIn(("initial", "second"), {(c["id"], c["head_run_id"]) for c in detail["conversations"]})
        self.assertEqual({(t["run_id"], t["task_id"], t["participation"]) for t in detail["tasks"]},
                         {("dm", "dm", "solo"), ("initial", "initial-0", "member"), ("initial", "initial-1", "member")})
        for row in detail["tasks"]:
            run = self.studio.detail(row["run_id"])
            self.assertIn(row["task_id"], {task["task_id"] for task in run["tasks"]})
            self.assertEqual(run["conversation"]["role_keys"]["dev"], dev_key)
        lead, = self.studio.role_directory("writer")["tasks"]
        self.assertEqual((lead["task_id"], lead["participation"]), ("initial", "lead"))
        reviewer, = self.studio.role_directory("reviewer")["tasks"]
        self.assertEqual((reviewer["run_id"], reviewer["participation"]), ("second", "member"))

    def test_name_collisions_and_matching_unbound_profiles_never_bind_implicitly(self):
        self.record("old", assignments=["dev"])
        self.record("same", assignments=["dev"])
        self.record("different", assignments=["dev"], profiles=[{**self.dev, "prompt": "Different duty."}])
        self.record("custom-writer", kind="dm", members=["writer"], recipient="writer", mode="single",
                    profiles=[{"id": "writer", "name": "Writer", "prompt": "Specific role."}])
        self.studio.add_profile(self.dev)
        self.studio.role_memory.save("dev", kind="memory", text="Private-identity-note")
        self.record("saved", kind="dm", members=["dev"], recipient="dev", mode="single", saved=["dev"])
        self.studio.edit_profile("dev", {"previous": {"name": self.dev["name"], "prompt": self.dev["prompt"]},
                                        "name": "Software Engineer", "prompt": "New duty."})
        cards = [r for r in self.studio.role_directory()["roles"] if r["id"] == "dev"]
        self.assertEqual(len(cards), 3)
        for card in cards:
            data = self.studio.role_directory(card["key"])
            actual = {t["run_id"] for t in data["tasks"]}
            if card["source"] == "saved":
                self.assertEqual((card["name"], actual), ("Software Engineer", {"saved"}))
            else:
                self.assertEqual(actual, {"old", "same"} if card["prompt"] == self.dev["prompt"] else {"different"})
                self.assertNotIn("Private-identity-note", str(data))
        self.assertEqual(set(self.keys("writer")), {"builtin", "historical"})
        self.assertNotIn("custom-writer", {t["run_id"] for t in self.studio.role_directory("writer")["tasks"]})

    def test_explicit_legacy_link_and_superagent_identity(self):
        self.record("legacy", kind="dm", members=["dev"], recipient="dev", mode="single")
        path = self.studio.runs_dir / "legacy" / "studio.json"
        meta = json.loads(path.read_text())
        del meta["thread"]["saved_roles"]
        path.write_text(json.dumps(meta))
        old = self.keys("dev")["historical"]
        self.record("modern", kind="dm", members=["dev"], recipient="dev", mode="single")
        self.studio.add_profile(self.dev)
        self.assertEqual({r["id"] for r in self.studio.role_library("dev")["unbound_conversations"]},
                         {"legacy", "modern"})
        self.assertEqual(self.studio.role_directory("dev")["tasks"], [])
        self.studio.link_profile("dev", {"room_id": "legacy", "expected_head": "legacy"})
        self.assertEqual([row["run_id"] for row in self.studio.role_directory("dev")["tasks"]], ["legacy"])
        self.assertEqual([row["run_id"] for row in self.studio.role_directory(old)["tasks"]], ["modern"])
        self.studio.link_profile("dev", {"room_id": "modern", "expected_head": "modern"})
        self.assertEqual(self.keys("dev"), {"saved": "dev"})
        self.assertEqual({row["run_id"] for row in self.studio.role_directory("dev")["tasks"]},
                         {"legacy", "modern"})
        self.assertNotEqual(old, self.studio.detail("legacy")["conversation"]["role_keys"]["dev"])
        self.record("assistant", kind="dm", members=["worker"], recipient="worker", mode="single",
                    profiles=[], superagent=True)
        self.assertEqual(self.studio.detail("assistant")["conversation"]["role_keys"], {})
        worker = self.studio.role_directory("worker")
        self.assertEqual((worker["tasks"], worker["conversations"]), ([], []))

    def test_taskless_roster_reads_only_metadata_and_role_history_survives_a_damaged_task_db(self):
        self.record("good", assignments=["dev"])
        self.record("bad", assignments=["dev"])
        (self.studio.runs_dir / "bad" / "tasks.sqlite3").write_bytes(b"damaged")
        with patch.object(self.studio, "_record_snapshot", side_effect=AssertionError("list read TaskStore")):
            key = self.keys("dev")["historical"]
        detail = self.studio.role_directory(key)
        self.assertEqual(detail["incomplete_run_ids"], ["bad"])
        self.assertEqual([t["run_id"] for t in detail["tasks"]], ["good"])


class DirectoryHTTPTest(unittest.TestCase):
    setUp = chat_http.StudioChatHTTPTest.setUp
    get = chat_http.StudioChatHTTPTest.get
    exchange = chat_http.StudioChatHTTPTest.exchange

    def test_native_legacy_team_work_remains_reachable_without_a_synthetic_group_chat(self):
        import uuid
        from time import monotonic, sleep

        status, result = self.exchange("POST", "/api/runs", {"request_id": str(uuid.uuid4()), "mode":"team-demo"})
        self.assertEqual(status, 202, result)
        rid = result["run"]["id"]
        deadline = monotonic() + 5
        while monotonic() < deadline:
            if self.get("/api/runs/" + rid)["run"]["status"] == "completed":
                break
            sleep(0.01)
        listed = self.get("/api/directory/roles")["roles"]
        self.assertEqual(len(listed), 4)
        writer = self.get("/api/directory/roles/writer")
        task, = writer["tasks"]
        self.assertEqual((task["status"], task["kind"], task["conversation_id"], task["run_id"]),
                         ("completed", "run", None, rid))
        self.assertEqual(writer["conversations"], [])
        self.assertEqual(self.exchange("GET", "/api/directory/roles/missing")[0], 404)
