import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.studio_service import StudioError, StudioService
from tests.test_studio_conversations import RecordingProvider, fresh, msg, reply
from tests.test_studio_service import finished


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
