"""Use real booking records across a version handoff and a downloaded ZIP."""
import io
import hashlib
import json
import os
import shutil
import sqlite3
import unittest
import zipfile
from pathlib import Path

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
        for value in ("sk-" + "ab012cde" * 4, task + "extra", "extra" + task, "sk-short-" + task,
                      self.service.settings.api_key):
            report.write_text(task + " " + value)
            with self.subTest(), self.assertRaisesRegex(SnapshotError, "服务凭据"):
                self.copy()
        from miniagent.project_data import validate_project_data
        with self.assertRaisesRegex(ValueError, "服务凭据"):
            validate_project_data("factcheck.md", task.encode(), secret=task[2:], reject_credentials=True)

    def test_published_manufacturer_news_title_in_handoff(self):
        from miniagent.project_data import validate_project_data
        title = "sk-hynix-ships-samples-of-12-layer-next-gen-hbm4e-2"
        url = "https://news.skhynix.com/en/" + title + "/"
        other_url = "https://news.skhynix.com/en/sk-hynix-ships-world-first-12-layer-hbm4-samples-to-customers/"
        notes = self.agent / "research_notes.md"
        content = ("# Research notes\n\nSource: `" + url + "`\n\n[Manufacturer]( " + url +
                   "?utm_source=newsletter#press )\nSecond source: " + other_url + "\n").encode()
        notes.write_bytes(content)
        self.assertEqual((self.copy() / notes.name).read_bytes(), content)
        synthetic = "sk-" + "z" * 36
        for value in (title, "https://example.org/" + title + "/", url.replace("/en/", "/other/"),
                      "https://example.org/?redirect=" + url, "https://example.org/?redirect=(" + url + ")", url.replace("samples", "ab12cd34ef56ab12cd34"),
                      url.replace("samples", "SAMPLES"), url.replace("samples", "samples_of"),
                      url.replace("hbm4e", synthetic), url.replace("next-gen", "sk-proj-next-generation-samples-for-advanced-memory"), url + "?access=" + synthetic,
                      url + "#section-" + synthetic, url + " " + synthetic, url + " " + title):
            with self.subTest():
                with self.assertRaisesRegex(ValueError, "服务凭据.*第 3 行") as rejected:
                    validate_project_data(notes.name, ("One\nTwo\n" + value).encode(), reject_credentials=True)
                self.assertNotIn(synthetic, str(rejected.exception))
                self.assertNotIn(value, str(rejected.exception))
        with self.assertRaisesRegex(ValueError, "第 3 行"):
            validate_project_data(notes.name, content, secret=title, reject_credentials=True)
        # The exception belongs to text citations. Binary SQLite still scans
        # the complete image (including source URLs saved in records).
        connection = sqlite3.connect(self.file)
        with connection:
            connection.execute("INSERT INTO bookings VALUES(?,0,0)", (url,))
        connection.close()
        with self.assertRaisesRegex(SnapshotError, "数据库含可识别的服务凭据"):
            self.copy()

    def test_only_corroborated_title_words_in_source_receipt_url_fields(self):
        from miniagent.project_data import validate_project_data

        def archive(record):
            data = json.dumps(record, ensure_ascii=False, indent=2).encode()
            return "sources/" + hashlib.sha256(data).hexdigest() + ".json", data

        def discovery(title, url, **extra):
            return {"kind":"source_discovery", "results":[{"title":title, "url":url}], **extra}

        title = "Memory Runs Dry: Samsung and SK Hynix Drop Below 10-Day Supply as HBM4 Devours Capacity"
        url = "https://www.techtimes.com/articles/326859/memory-runs-dry-samsung-sk-hynix-drop-below-10-day-supply-hbm4-devours-capacity.htm"
        source = discovery(title,url)
        name, data = archive(source)
        validate_project_data(name, data, reject_credentials=True)
        # The government API embeds title/html_url pairs inside a fetched JSON body.
        city = "https://www.federalregister.gov/documents/azur-air-krasnoyarsk-russia-660020-order-renewing-temporary-denial-of"
        fetched = {"kind":"fetched_source", "content_type":"application/json", "text":json.dumps({"results":[
            {"title":"Azur Air, Krasnoyarsk, Russia 660020; Order Renewing Temporary Denial of Export Privileges",
             "html_url":city}]})}
        validate_project_data(*archive(fetched), reject_credentials=True)
        synthetic = "sk-" + "x" * 40
        for record in (discovery("Some other story",url), discovery(title,url+"?token="+synthetic),
                       discovery(title,url,notes=url), discovery(title,url,other={"url":url}),
                       discovery(title,url,token=synthetic),
                       discovery("SK Hynix and Investment Risk",url.replace("capacity.htm",synthetic+".htm")),
                       discovery(" ".join(url.split("/")[-1].split("-")),
                                 url.replace("sk-hynix-", "sk-proj-")),
                       {**fetched,"text":fetched["text"].replace("krasnoyarsk","mansiysk")}):
            with self.subTest(), self.assertRaisesRegex(ValueError,"服务凭据"):
                validate_project_data(*archive(record), reject_credentials=True)
        for path in (Path(name).name, "other/"+Path(name).name, "sources/"+"0"*64+".json"):
            with self.subTest(), self.assertRaisesRegex(ValueError,"服务凭据"):
                validate_project_data(path, data, reject_credentials=True)
        with self.assertRaisesRegex(ValueError,"服务凭据"):
            validate_project_data(name, data, secret=url[url.index("sk-"):], reject_credentials=True)

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
