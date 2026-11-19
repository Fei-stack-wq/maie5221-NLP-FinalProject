"""Use real booking records across a version handoff and a downloaded ZIP."""
import io
import hashlib
import json
import os
import shutil
import sqlite3
import unittest
import zipfile

from miniagent.config import Settings
from miniagent.studio_conversations import SnapshotError, copy_project
from miniagent.studio_service import StudioError, StudioService, _EXTENSIONS
from tests.support import cleanup, make_temp_dir
from tests.test_studio_service import finished, request


class ProjectDataTest(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir("project-data")
        self.service = StudioService(self.root / "runs", Settings(api_key="binary-secret-key"))
        self.run_id = self.service.start(request())["id"]
        finished(self.service, self.run_id)
        self.run = self.root / "runs" / self.run_id
        self.agent = self.run / "agents" / "worker"
        self.file = self.agent / "bookings.sqlite"
        connection = sqlite3.connect(self.file)
        with connection:
            connection.execute("CREATE TABLE bookings(room, minutes, cancelled)")
            connection.executemany("INSERT INTO bookings VALUES(?,?,?)", [("A", 30, 1), ("A", 45, 0), ("B", 60, 0)])
        connection.close()

    def tearDown(self):
        self.service.close()
        cleanup(self.root)

    def copy(self):
        target = self.root / "next"
        copy_project(self.run, "worker", target, _EXTENSIONS, secret=self.service.settings.api_key)
        return target

    def test_sqlite_and_csv_round_trip_and_ignore_generated_cache(self):
        csv = self.agent / "bookings.csv"
        csv.write_bytes(b"room,minutes,cancelled\r\nA,30,1\r\n")
        cache = self.agent / "__pycache__"
        cache.mkdir()
        (cache / "cli.pyc").write_bytes(b"\x00")
        target = self.copy()
        self.assertFalse((target / "__pycache__").exists())
        self.assertEqual((target / "bookings.csv").read_bytes(), csv.read_bytes())
        connection = sqlite3.connect(target / self.file.name)
        try:
            self.assertEqual(connection.execute("SELECT SUM(minutes) FROM bookings WHERE NOT cancelled").fetchone(), (105,))
        finally:
            connection.close()
        with zipfile.ZipFile(io.BytesIO(self.service.artifact_bundle(self.run_id))) as package:
            self.assertEqual(package.read("bookings.sqlite"), self.file.read_bytes())
            self.assertEqual(package.read("bookings.csv"), csv.read_bytes())
        self.assertEqual(self.service.artifact(self.run_id, "agents/worker/bookings.csv")["content"], csv.read_bytes().decode())
        with self.assertRaises(StudioError) as caught:
            self.service.artifact(self.run_id, "agents/worker/bookings.sqlite")
        self.assertEqual(caught.exception.code, "binary_artifact")

    def test_live_journal_refused_by_copy_and_zip(self):
        self.file.with_suffix(".sqlite-wal").write_bytes(b"pending changes")
        with self.assertRaisesRegex(SnapshotError, "checkpoint"):
            self.copy()
        with self.assertRaises(StudioError) as caught:
            self.service.artifact_bundle(self.run_id)
        self.assertEqual(caught.exception.code, "database_busy")

    def test_reject_malformed_or_sensitive_database(self):
        connection = sqlite3.connect(self.file)
        with connection:
            connection.execute("INSERT INTO bookings VALUES(?,0,0)", (self.service.settings.api_key,))
        connection.close()
        for label in ("secret", "malformed"):
            with self.subTest(label=label):
                if label == "malformed":
                    self.file.write_bytes(b"looks like an arbitrary binary blob")
                with self.assertRaises(SnapshotError):
                    self.copy()
                with self.assertRaises(StudioError) as caught:
                    self.service.artifact_bundle(self.run_id)
                self.assertEqual(caught.exception.code, "unsupported_file")

    def test_copied_control_db_and_previous_provider_key_cannot_be_exported(self):
        self.file.unlink()
        shutil.copyfile(self.run / "tasks.sqlite3", self.file)
        with self.assertRaisesRegex(StudioError, "控制数据库"):
            self.service.room_bundle(self.run_id)
        self.file.unlink()
        db = sqlite3.connect(self.file)
        try:
            db.execute("PRAGMA secure_delete=OFF")
            db.execute("CREATE TABLE receipts(value)")
            # Large deleted rows leave bytes in free pages.
            db.execute("INSERT INTO receipts VALUES(?)", ("sk-" + "1234ABCD" * 10 + "x"*8000,))
            db.commit()
            db.execute("DELETE FROM receipts")
            db.commit()
        finally:
            db.close()
        self.assertIn(b"sk-1234", self.file.read_bytes())
        with self.assertRaisesRegex(StudioError, "服务凭据"):
            self.service.room_bundle(self.run_id)

    def test_control_database_and_hard_link_cannot_be_downloaded(self):
        control = self.run / "tasks.sqlite3"
        os.link(control, self.agent / "alias.db")
        with zipfile.ZipFile(io.BytesIO(self.service.artifact_bundle(self.run_id))) as package:
            self.assertNotIn("alias.db", package.namelist())
            self.assertNotIn("tasks.sqlite3", package.namelist())
        with self.assertRaises(SnapshotError):
            self.copy()

    def test_factcheck_handoff_can_cite_a_studio_task_without_masking_credentials(self):
        task = "task-" + "ab012cde" * 4
        report = self.agent / "factcheck.md"
        report.write_text("Reviewed (" + task + ").")
        self.assertEqual((self.copy() / report.name).read_text(), report.read_text())
        for value in ("sk-" + "ab012cde" * 4, task + "extra", "extra" + task, self.service.settings.api_key):
            report.write_text(task + " " + value)
            with self.subTest(), self.assertRaisesRegex(SnapshotError, "服务凭据"):
                self.copy()
        from miniagent.project_data import validate_project_data
        with self.assertRaisesRegex(ValueError, "服务凭据"):
            validate_project_data("factcheck.md", task.encode(), secret=task[2:], reject_credentials=True)

    def test_member_or_legacy_text_snapshot_rejects_credentials_before_transfer(self):
        project_file = self.agent / "settings.txt"
        for fake in ("sk-" + "x" * 40, self.service.settings.api_key):
            with self.subTest(), self.assertRaisesRegex(SnapshotError, "服务凭据") as rejected:
                project_file.write_text("key=" + fake, encoding="utf-8")
                self.copy()
            self.assertNotIn(fake, str(rejected.exception))
            self.assertFalse((self.root / "next" / project_file.name).exists())

    def test_protocol_source_anchor_can_be_copied_but_embedded_credentials_still_block(self):
        from miniagent.project_data import validate_project_data
        title = "Re Task Configuration Fields Representation"
        slug = title.lower().replace(" ", "-")
        link = f"[{title}](#312-{slug})"
        body = json.dumps({"kind":"fetched_source", "text":f"### 3.1.2 {title}\n\nSee {link}."}).encode()
        sources = self.agent / "sources"
        sources.mkdir()
        source = sources / (hashlib.sha256(body).hexdigest() + ".json")
        source.write_bytes(body)
        self.assertEqual((self.copy() / "sources" / source.name).read_bytes(), body)

        key = "sk-" + "a" * 40
        nested = "Task Overview SK Detailed Configuration Transport Requirements"
        opaque_task = "task-" + "abc123" * 6
        unsafe = [key, "api_key=ta" + key, f"[details](https://example.invalid/{key})",
                  f"[{title}](https://example.invalid/#{slug})", f"[unrelated](#312-{slug})",
                  "api_key=" + slug, opaque_task, f"[{nested}](#{nested.lower().replace(' ', '-')})"]
        for extra in unsafe:
            source.write_bytes(json.dumps({"kind":"fetched_source", "text":link + "\n" + extra}).encode())
            with self.subTest(), self.assertRaisesRegex(SnapshotError, "服务凭据") as caught:
                self.copy()
            self.assertNotIn(key, str(caught.exception))
        with self.assertRaisesRegex(ValueError, "服务凭据"):
            validate_project_data("source.json", body, secret=slug[slug.index("task-")+2:], reject_credentials=True)
