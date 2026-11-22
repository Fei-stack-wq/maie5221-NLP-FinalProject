import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.studio_service import StudioError, StudioService
from tests.test_studio_conversations import RecordingProvider, fresh, msg, reply
from tests.test_studio_service import finished
from tests.test_studio_superagent import tool


class KnowledgeProjectTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.runs = self.root / "runs"
        self.service = StudioService(self.runs, Settings(api_key="test-credential"))
        self.service.add_profile({"id":"maintainer", "name":"维护员", "prompt":"Manage the project."})

    def tearDown(self):
        self.service.close()

    def restart(self):
        self.service.close()
        self.service = StudioService(self.runs, Settings(api_key="test-credential"))

    def turn(self, parent=None, *, new=False, workspace=None, question=False):
        if parent:
            conv = reply(parent, "maintainer", "question" if question else "edit")
            if new:
                conv["inherit_files"] = False
        else:
            conv = fresh("maintainer", members=["maintainer", "worker"])
        options = {"workspace_path":workspace} if workspace is not None else {}
        script = RecordingProvider([{"text":"本轮已结束。"}])
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=script):
            result = self.service.start(msg("说明当前项目", conv, **options))
            result = finished(self.service, result["id"])
        self.assertTrue(script.requests)
        return result["run"]["id"], result["conversation"], json.dumps(script.requests[0][0], ensure_ascii=False)

    def read_meta(self, run):
        return json.loads((self.runs / run / "studio.json").read_text())

    def memory(self, project, text):
        self.service.role_memory.save("maintainer", kind="memory", scope="project", project=project, text=text)

    def test_one_room_switches_project_and_next_turn_finds_live_role_knowledge(self):
        first, room, _ = self.turn()
        self.assertEqual(room["knowledge_project_id"], first)
        self.memory(first, "PRIVATE-OLD-PROJECT-44")
        second, room, prompt = self.turn(first, question=True)
        self.assertIn("PRIVATE-OLD-PROJECT-44", prompt)
        self.assertEqual(room["knowledge_project_id"], first)
        new, room, prompt = self.turn(second, new=True)
        self.assertEqual((room["id"], room["knowledge_project_id"]), (first, new))
        self.assertNotIn("PRIVATE-OLD-PROJECT-44", prompt)
        self.assertEqual(self.read_meta(new)["thread"]["knowledge_project_id"], new)
        self.memory(new, "PRIVATE-NEW-PROJECT-55")
        self.restart()
        followup, room, prompt = self.turn(new, question=True)
        self.assertEqual(self.read_meta(followup)["thread"]["knowledge_project_id"], new)
        self.assertIn("PRIVATE-NEW-PROJECT-55", prompt)
        self.assertNotIn("PRIVATE-OLD-PROJECT-44", prompt)
        self.assertEqual(room["knowledge_project_id"], new)

    def test_two_rooms_sharing_role_and_folder_keep_run_sourced_notes_local(self):
        folder = self.root / "shared"
        folder.mkdir()
        first, _, _ = self.turn(workspace=str(folder))
        library = self.service.role_memory
        entry = library.save("maintainer", kind="memory", text="FIRST-GROUP-ONLY", scope="project",
                             project=str(folder), source="agent", source_run=first, status="candidate")
        library.set_status("maintainer", entry["id"], "active", entry["revision"])
        self.memory(str(folder), "CURATED-FOLDER-NOTE")
        library.save("maintainer", kind="memory", text="PORTABLE-ROLE-PREFERENCE")
        self.restart()
        second, second_room, second_prompt = self.turn(workspace=str(folder))
        self.assertEqual(second_room["id"], second)
        self.assertNotIn("FIRST-GROUP-ONLY", second_prompt)
        for shared in ("CURATED-FOLDER-NOTE", "PORTABLE-ROLE-PREFERENCE"):
            self.assertIn(shared, second_prompt)

        def retrieve(parent):
            model = RecordingProvider([tool("recall_role_knowledge", query="FIRST-GROUP-ONLY"),
                                       tool("read_role_knowledge", entry_id=entry["id"]), {"text": "已检索。"}])
            with patch("miniagent.studio_service.DeepSeekProvider", return_value=model):
                turn = self.service.start(msg("查询知识", reply(parent, "maintainer")))
                finished(self.service, turn["id"])
            return [message["content"] for message in model.requests[-1][0] if message.get("role") == "tool"]

        hidden = retrieve(second)
        self.assertEqual(len(hidden), 2)
        self.assertNotIn("FIRST-GROUP-ONLY", str(hidden))
        self.assertIn("没有这条", hidden[1])
        visible = retrieve(first)
        self.assertEqual(len(visible), 2)
        self.assertTrue(all("FIRST-GROUP-ONLY" in result for result in visible))

    def test_old_metadata_after_explicit_reset_recovers_latest_project_identity(self):
        first, _, _ = self.turn()
        reset, _, _ = self.turn(first, new=True)
        last, _, _ = self.turn(reset, question=True)
        for run_id in [first, reset, last]:
            record = self.read_meta(run_id)
            del record["thread"]["knowledge_project_id"]
            (self.runs / run_id / "studio.json").write_text(json.dumps(record))
        self.memory(first, "PRE-RESET-PRIVATE-VERSION")
        self.memory(reset, "POST-RESET-PRIVATE-VERSION")
        self.restart()
        current, room, prompt = self.turn(last)
        self.assertEqual(self.read_meta(current)["thread"]["knowledge_project_id"], reset)
        self.assertEqual(room["knowledge_project_id"], reset)
        self.assertIn("POST-RESET-PRIVATE-VERSION", prompt)
        self.assertNotIn("PRE-RESET-PRIVATE-VERSION", prompt)

    def test_normalized_workspace_overrides_run_id_and_switch_can_choose_new_directory(self):
        a, b = self.root / "project A", self.root / "project B"
        a.mkdir()
        b.mkdir()
        self.memory(str(a), "PROJECT-A-AT-PATH")
        self.memory(str(b), "PROJECT-B-AT-PATH")
        first, room, prompt = self.turn(workspace=str(a) + "/.")
        self.assertEqual(room["knowledge_project_id"], str(a))
        self.assertIn("PROJECT-A-AT-PATH", prompt)
        alias = str(a) + "/../project A"
        second, room, _ = self.turn(first, workspace=alias)
        self.assertEqual(self.read_meta(second)["thread"]["knowledge_project_id"], str(a))
        with self.assertRaises(StudioError):
            self.turn(second, workspace=str(b))
        switched, room, prompt = self.turn(second, new=True, workspace=str(b))
        self.assertEqual((room["id"], room["knowledge_project_id"]), (first, str(b)))
        self.assertIn("PROJECT-B-AT-PATH", prompt)
        self.assertNotIn("PROJECT-A-AT-PATH", prompt)
        _, room, _ = self.turn(switched, question=True)
        self.assertEqual((room["workspace_path"], room["knowledge_project_id"]), (str(b), str(b)))
