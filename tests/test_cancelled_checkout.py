import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.studio_service import StudioError, StudioService
from tests.test_studio_conversations import RecordingProvider, tool
from tests.test_studio_crew import roster
from tests.test_studio_service import finished, request
from tests.test_studio_steering import PausedModel


class CancelledCheckoutTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.service = StudioService(Path(directory) / "runs", Settings(api_key="test-sentinel"))
        self.addCleanup(self.service.close)

    def cancelled(self):
        # The lead has one settled stage, then receives a real stop while coordinating.
        script = [tool("delegate_task", assignee="research", objective="Write notes",
                       deliverable_paths=["stage.md"], remaining_roles=[]),
                  tool("write_file", path="stage.md", content="published research"),
                  {"text": "Notes ready."}, {"text": "Paused."}]
        model = PausedModel(script, pause_at=3)
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=model):
            rid = self.service.start(request("team", objective="Produce the two documents",
                                            conversation=roster("lead", "research"),
                                            acceptance_files=["stage.md", "final.md"]))["id"]
            self.assertTrue(model.gate.entered.wait(5))
            self.service.cancel(rid)
            model.gate.release.set()
            detail = finished(self.service, rid)
        self.assertEqual(detail["run"]["status"], "cancelled")
        self.assertEqual(detail["conversation"]["artifact_run_id"], rid)
        return rid

    def continue_group(self, parent, inherit=True):
        return self.service.start(request("single", objective="Continue from the existing research.",
            conversation={"parent_run_id": parent, "recipient": "lead", "intent": "edit", "inherit_files": inherit}))

    def test_continue_cancelled_group_uses_only_published_bytes_even_when_the_lead_copy_changed(self):
        rid = self.cancelled()
        workspace = self.service.runs_dir / rid / "agents/lead"
        (workspace / "stage.md").write_text("unpublished change")
        (workspace / "private.md").write_text("unpublished working note")
        model = RecordingProvider([tool("read_file", path="stage.md"), {"text": "Done."}])
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=model):
            resumed = self.continue_group(rid)
            detail = finished(self.service, resumed["id"])
        root = self.service.runs_dir / resumed["id"]
        self.assertEqual((root / "agents/lead/stage.md").read_text(), "published research")
        self.assertFalse((root / "agents/lead/private.md").exists())
        self.assertEqual(json.loads((root / "inherited-inputs.json").read_text()),
                         {"stage.md": hashlib.sha256(b"published research").hexdigest()})
        self.assertEqual(detail["conversation"]["turns"][-1]["base_run_id"], rid)
        self.assertEqual(detail["conversation"]["turns"][0]["status"], "cancelled")

    def test_tampered_public_archive_rejects_admission_and_explicit_new_project_starts_empty(self):
        rid = self.cancelled()
        archive, = (self.service.runs_dir / rid).glob("handoffs/*/project/stage.md")
        archive.write_text("tampered")
        with self.assertRaises(StudioError):
            self.continue_group(rid)
        self.assertEqual(len(self.service.detail(rid)["conversation"]["turns"]), 1)
        model = RecordingProvider([{"text": "New project."}])
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=model):
            run = self.continue_group(rid, inherit=False)
            finished(self.service, run["id"])
        self.assertFalse((self.service.runs_dir / run["id"] / "agents/lead/stage.md").exists())
