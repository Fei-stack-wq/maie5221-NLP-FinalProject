import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from miniagent import studio_workspace as ws
from miniagent.config import Settings
from miniagent.studio_service import StudioService
from tests.test_studio_service import finished, request


class WorkspacePublishRaceTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.local = self.root / "project"
        self.local.mkdir()
        self.private = self.root / "private"

    def test_concurrent_create_cannot_be_replaced_after_validation(self):
        (self.local / "a.md").write_text("prior")
        baseline = ws.checkout(self.local, self.private, {".md"}, "")
        (self.private / "a.md").write_text("agent")
        (self.private / "b.md").write_text("agent")
        link = os.link
        def simultaneous_create(*args, **kwargs):
            (self.local / "b.md").write_text("user's new file")
            return link(*args, **kwargs)
        with patch.object(ws.os, "link", side_effect=simultaneous_create), self.assertRaises(ws.WorkspaceError):
            ws.publish(self.local, self.private, baseline, {".md"}, "")
        self.assertEqual((self.local / "a.md").read_text(), "prior")
        self.assertEqual((self.local / "b.md").read_text(), "user's new file")

    def test_stored_baseline_uses_same_file_scope_as_checkout(self):
        for name in ("../outside.md", "/root.md", ".git/settings.md", "ignored.bin", "bad//path.md"):
            with self.subTest(name=name), self.assertRaises(ws.WorkspaceError):
                ws._baseline_files({"files": {name: "0" * 64}}, {".md"})

    def test_database_opened_while_staging_blocks_publish(self):
        data = self.local / "book.db"
        db = sqlite3.connect(data)
        db.execute("create table bookings (name)")
        db.commit()
        db.close()
        baseline = ws.checkout(self.local, self.private, {".db"}, "")
        child = sqlite3.connect(self.private / "book.db")
        child.execute("insert into bookings values ('new')")
        child.commit()
        child.close()
        original = data.read_bytes()
        def open_journal(_fd):
            Path(str(data) + "-journal").write_bytes(b"user transaction")
        with patch.object(ws.os, "fsync", side_effect=open_journal), self.assertRaises(ws.WorkspaceError):
            ws.publish(self.local, self.private, baseline, {".db"}, "")
        self.assertEqual(data.read_bytes(), original)

    def test_rollback_continues_if_an_applied_file_was_edited_by_user(self):
        for path in ("a.md", "b.md"):
            (self.local / path).write_text("prior")
        baseline = ws.checkout(self.local, self.private, {".md"}, "")
        for path in ("a.md", "b.md", "z.md"):
            (self.private / path).write_text("agent")
        def interrupted(*args, **kwargs):
            (self.local / "b.md").write_text("user edit after partial publish")
            raise OSError("write failure")
        with patch.object(ws.os, "link", side_effect=interrupted), self.assertRaisesRegex(ws.WorkspaceError, "b.md"):
            ws.publish(self.local, self.private, baseline, {".md"}, "")
        self.assertEqual((self.local / "a.md").read_text(), "prior")
        self.assertEqual((self.local / "b.md").read_text(), "user edit after partial publish")
        self.assertFalse((self.local / "z.md").exists())

    def test_post_execution_cancel_and_internal_publish_failure_are_terminal(self):
        service = StudioService(self.root / "runs", Settings())
        self.addCleanup(service.close)
        execute = service._execute
        def cancel_after_execute(run_id, request):
            outcome = execute(run_id, request)
            service.cancel(run_id)
            return outcome
        with patch.object(service, "_execute", side_effect=cancel_after_execute):
            run = service.start(request(workspace_path=str(self.local)))
            detail = finished(service, run["id"])
        self.assertEqual(detail["run"]["status"], "cancelled")
        self.assertFalse((self.local / "notes.md").exists())
        with patch.object(ws, "publish", side_effect=RuntimeError("sensitive internal data")):
            run = service.start(request(workspace_path=str(self.local)))
            detail = finished(service, run["id"])
        self.assertEqual(detail["run"]["status"], "failed")
        self.assertNotIn("sensitive internal data", detail["run"]["error"])
        self.assertEqual(detail["run"]["workspace_delivery"]["status"], "failed")
