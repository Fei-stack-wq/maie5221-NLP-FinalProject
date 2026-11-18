import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.studio_service import StudioError, StudioService
from miniagent.studio_workspace import checkout, publish, WorkspaceError
from tests.test_studio_conversations import RecordingProvider, tool
from tests.test_studio_service import request, finished


class LocalWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.local = root / "my project"
        self.local.mkdir()
        self.private = root / "private"
        self.svc = StudioService(root / "runs", Settings(api_key="fake-service-key"))
        self.addCleanup(self.svc.close)

    def test_files_conflicts_and_concurrent_edits(self):
        for name in ["a.md", "b.md", "unrelated.md", ".hidden.md", "ignore.bin"]:
            (self.local / name).write_text(name)
        baseline = checkout(self.local, self.private, {".md"}, "")
        self.assertEqual(set(baseline["files"]), {"a.md", "b.md", "unrelated.md"})
        (self.private / "a.md").write_text("agent A")
        (self.private / "b.md").unlink()
        (self.private / "docs").mkdir()
        (self.private / "docs/a.md").write_text("new")
        (self.local / "b.md").write_text("human revision")
        with self.assertRaises(WorkspaceError):
            publish(self.local, self.private, baseline, {".md"}, "")
        self.assertEqual((self.local / "a.md").read_text(), "a.md")
        self.assertFalse((self.local / "docs").exists())
        (self.local / "b.md").write_text("b.md")
        (self.local / "unrelated.md").write_text("edited in GUI")
        self.assertEqual(publish(self.local, self.private, baseline, {".md"}, ""), ["a.md", "b.md", "docs/a.md"])
        self.assertEqual((self.local / "unrelated.md").read_text(), "edited in GUI")
        self.assertFalse((self.local / "b.md").exists())
        self.assertEqual((self.local / "docs/a.md").read_text(), "new")

    def test_symlink_to_other_directory_and_replaced_workspace_fail_closed(self):
        baseline = checkout(self.local, self.private, {".md"}, "")
        (self.private / "sub").mkdir()
        (self.private / "sub/file.md").write_text("new")
        outside = self.local.parent / "outside"
        outside.mkdir()
        (self.local / "sub").symlink_to(outside)
        with self.assertRaises((OSError, WorkspaceError)):
            publish(self.local, self.private, baseline, {".md"}, "")
        self.assertFalse((outside / "file.md").exists())
        self.local.rename(self.local.with_name("old"))
        self.local.mkdir()
        with self.assertRaises(WorkspaceError):
            publish(self.local, self.private, baseline, {".md"}, "")

    def test_chat_links_preserve_versions_follow_local_edits_and_questions_are_read_only(self):
        first = self.svc.start(request(workspace_path=str(self.local)))
        detail = finished(self.svc, first["id"])
        self.assertEqual(detail["run"]["status"], "completed")
        filename = detail["conversation"]["turns"][0]["artifacts"][0]["path"]
        self.assertEqual((self.local / "notes.md").read_bytes(), self.svc.download_artifact(first["id"], filename))
        (self.local / "notes.md").write_text("edited locally")
        provider = RecordingProvider([tool("read_file", path="notes.md"), {"text": "Reviewed"}])
        def submit(parent, intent, provider, **extra):
            with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider):
                run = self.svc.start(request("single", objective="read this", conversation={
                    "parent_run_id": parent, "intent": intent, "recipient": "worker"}, **extra))
                return finished(self.svc, run["id"])
        question = submit(first["id"], "question", provider)
        self.assertIn("edited locally", str(provider.requests[-1][0]))
        self.assertEqual(question["conversation"]["turns"][-1]["artifacts"], [])
        self.assertNotIn(b"edited locally", self.svc.download_artifact(first["id"], filename))
        update = RecordingProvider([tool("write_file", path="new.md", content="second turn, with a new project document"), {"text": "done"}])
        second = submit(question["run"]["id"], "edit", update, acceptance_files=["new.md"])
        self.assertEqual(second["run"]["status"], "completed", (second["run"], second["acceptance"]))

        self.assertEqual(second["conversation"]["workspace_path"], str(self.local))
        self.assertEqual([a["name"] for a in second["conversation"]["turns"][-1]["artifacts"]], ["new.md"])
        self.assertEqual((self.local / "new.md").read_text(), "second turn, with a new project document")
        self.assertEqual((self.local / "notes.md").read_text(), "edited locally")

    def test_explicit_recovery_uses_validated_member_checkpoint_when_public_project_empty(self):
        from tests.test_studio_crew import roster, assign, write
        first = RecordingProvider([assign("author", "make a draft"), write("records.json", '[{"room":"A"}]'),
                                   {"text": "[BLOCKED] needs independent test"}, {"text": "[BLOCKED] incomplete"}])
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=first):
            run = self.svc.start(request("team", objective="create", conversation=roster("lead", "author"),
                                         workspace_path=str(self.local)))
            detail = finished(self.svc, run["id"])
        self.assertEqual(detail["run"]["status"], "failed")
        self.assertFalse((self.local / "records.json").exists())
        self.assertEqual(detail["conversation"]["turns"][-1]["artifacts"], [])
        model = RecordingProvider([tool("read_file", path="records.json"), {"text": "recovered original data"}])
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=model):
            later = self.svc.start(request("single", objective="use original records", conversation={
                "parent_run_id": run["id"], "intent": "edit", "recipient": "lead", "recover_failed": True}))
            resumed = finished(self.svc, later["id"])
        self.assertIn('room', str(model.requests[-1][0]))
        self.assertEqual(resumed["conversation"]["turns"][-1]["recovery_source"]["role"], "author")
        self.assertEqual((self.local / "records.json").read_text(), '[{"room":"A"}]')

    def test_publish_conflict_is_visible_and_safe_to_recover(self):
        with patch("miniagent.studio_workspace.publish", side_effect=WorkspaceError("本地文件已发生变化")):
            run = self.svc.start(request(workspace_path=str(self.local)))
            detail = finished(self.svc, run["id"])
        self.assertEqual(detail["run"]["status"], "failed")
        self.assertEqual(detail["run"]["workspace_delivery"]["status"], "failed")
        self.assertEqual(detail["conversation"]["turns"][0]["artifacts"][0]["name"], "notes.md")
        self.assertFalse((self.local / "notes.md").exists())

    def test_control_directory_credential_and_hidden_paths(self):
        for invalid in (str(self.svc.runs_dir), str(self.svc.runs_dir.parent), "relative"):
            with self.subTest(invalid=invalid), self.assertRaises(StudioError):
                self.svc.start(request(workspace_path=invalid))
        (self.local / "leak.txt").write_text("fake-service-key")
        with self.assertRaises(StudioError):
            self.svc.start(request(workspace_path=str(self.local)))
