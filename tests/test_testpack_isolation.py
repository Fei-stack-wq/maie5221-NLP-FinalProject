import io
import json
import unittest
import zipfile
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.providers import ScriptedProvider
from miniagent.studio_service import StudioService
from tests.support import make_temp_dir, cleanup
from tests.test_studio_conversations import RecordingProvider, tool, fresh, msg, reply
from tests.test_studio_service import finished


class TestpackIsolationTest(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir("testpack-isolation")
        self.addCleanup(cleanup, self.root)
        self.service = StudioService(self.root / "runs", Settings(api_key="fixture"))
        self.addCleanup(self.service.close)

    def run_turn(self, conversation, script):
        provider = RecordingProvider(script)
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider):
            run = self.service.start(msg("deliver this task", conversation, acceptance_files=["result.md"]))
            detail = finished(self.service, run["id"])
        return detail, provider

    def test_continuation_inherits_but_new_experiment_cannot_pass_on_old_file(self):
        original = "Task one immutable evidence and experience."
        first, _ = self.run_turn({**fresh(), "memory_enabled": True}, [
            tool("write_file", path="result.md", content=original), {"text": "First task delivered."}])
        self.assertEqual(first["run"]["status"], "completed")
        first_id = first["run"]["id"]
        saved = self.service.artifact_bundle(first_id)
        second, _ = self.run_turn(reply(first_id), [tool("read_file", path="result.md"),
            tool("write_file", path="result.md", content="Task two has independently updated this output."), {"text": "Second task delivered."}])
        self.assertEqual(second["run"]["status"], "completed")
        with zipfile.ZipFile(io.BytesIO(self.service.artifact_bundle(first_id))) as unchanged:
            self.assertEqual(unchanged.read("result.md").decode(), original)
        third, provider = self.run_turn({**reply(second["run"]["id"]), "inherit_files": False},
                                        [{"text": "No new file."}, {"text": "Still no new file."}])
        self.assertEqual(third["run"]["status"], "failed")
        self.assertFalse(third["artifacts"])
        memory = json.loads((self.root / "runs" / third["run"]["id"] / "role-memory.json").read_text())
        self.assertTrue(all(not rows for rows in memory["roles"].values()))
        # A completely new room also has neither history nor role experiences.
        fourth, provider = self.run_turn({**fresh(), "memory_enabled": True}, [{"text": "No file here."}] * 2)
        self.assertEqual(fourth["run"]["status"], "failed")
        self.assertNotIn("First task delivered.", json.dumps(provider.requests))
        with zipfile.ZipFile(io.BytesIO(saved)) as archive:
            self.assertEqual(archive.read("result.md").decode(), original)
