"""Exercise migration, restart, CLI evaluation and exclusive data-directory access."""
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from miniagent.cli import ROOT, main
from miniagent.instance import session_lock
from miniagent.roles import Roles
from miniagent.studio import Studio
from tests.helpers import FakeProvider, Script, call, reply, settings
from tests.test_team import wait_for


class RebuildWorkflows(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_migration_preserves_legacy_sources_and_scopes(self):
        source = self.root / "original"
        source.mkdir()
        (source / "studio-profiles.json").write_text(json.dumps([{"id":"qa","name":"QA","prompt":"Review"}]))
        db = source / "studio-role-memory.sqlite3"
        with contextlib.closing(sqlite3.connect(db)) as conn:
            conn.executescript("""
                CREATE TABLE role_entries(role,id,scope,project,kind,origin_run,origin_source,active_revision);
                CREATE TABLE role_revisions(entry_id,revision,text,status);
                INSERT INTO role_entries VALUES ('qa','one','role','','skill','','agent',1),
                    ('qa','two','conversation','g-private','memory','g-private','user',1),
                    ('qa','three','role','','memory','','agent',1);
                INSERT INTO role_revisions VALUES ('one',1,'verify numbers','active'),
                    ('two',1,'personal data','active'),('three',1,'pending','candidate');
            """)
        note = source / "groups/g-private/agents/editor/memory.md"
        note.parent.mkdir(parents=True)
        note.write_text("Unreviewed draft note\n")
        before = {p:p.read_bytes() for p in source.rglob("*") if p.is_file()}
        roles = Roles(self.root / "new")
        self.assertEqual(roles.migrate(source), {"roles":2,"knowledge":3})
        self.assertEqual(roles.migrate(source), {"roles":0,"knowledge":0})
        self.assertEqual({p:p.read_bytes() for p in source.rglob("*") if p.is_file()}, before)
        self.assertEqual([x["id"] for x in roles.entries("qa",group="g-other")], ["one"])
        self.assertEqual({x["id"] for x in roles.entries("qa",group="g-private")}, {"one","two"})
        self.assertEqual(roles.entries("editor",group="g-private"), [])
        self.assertEqual(roles.entries("editor")[0]["scope"], "group:g-private")

    def test_restart_closes_interrupted_run_and_dispatches_fifo(self):
        script = Script(lead=[reply("first response"), reply("second response")])
        studio = Studio(settings(self.root),lambda _:FakeProvider(script))
        self.addCleanup(studio.close)
        gid = studio.create_group("Room",[{"id":"lead","name":"Lead","prompt":""}],"lead")["id"]
        store = studio.group(gid)
        interrupted = "t-000000000000"
        store.save_task({"id":interrupted,"status":"running","created_at":1})
        for tid,seq in [("t-aaaaaaaaaaaa",3),("t-bbbbbbbbbbbb",2)]:
            store.save_task({"id":tid,"primary":"lead","status":"queued","text":tid,"created_at":2,
                             "trigger_seq":seq,"options":{},"queued_message":{"text":tid,"attachments":[],"addressed":"lead"}})
        studio.restore_queue()
        wait_for(lambda: studio.state(gid)["live"] is None)
        self.assertEqual(store.load_task(interrupted)["status"], "interrupted")
        order = [e["task_id"] for e in store.chat() if e["kind"] == "task_started"]
        self.assertEqual(order, ["t-bbbbbbbbbbbb","t-aaaaaaaaaaaa"])
        self.assertEqual(store.load_task(order[0])["final_answer"], "first response")

    def test_cli_runs_all_six_template_inputs_in_three_persistent_groups(self):
        runs = self.root / "runs"
        rows, responses = [], {}
        for path in sorted((ROOT / "examples/teams").glob("scenario_*.json")):
            template = json.loads(path.read_text())
            key = path.stem
            marker = "offline fixture " + key
            checks = [{"kind":"contains","path":"marker.txt","text":marker}]
            for task in template["tasks"]:
                rows.append({"template":key, "group":key, "text":task["text"], "checks":checks})
            rows[-1]["checks"] = checks + [
                {"kind":"command","command":"echo external-check > marker.txt; exit 7"}]
            responses.setdefault(template["lead"], []).extend([
                reply("",call("write_file",path="marker.txt",content=marker)),reply("Fixture published"),
                reply("",call("read_file",path="marker.txt")),reply("Fixture reopened")])
        source = self.root / "suite.jsonl"
        source.write_text("\n".join(json.dumps(r) for r in rows))
        provider = FakeProvider(Script(**responses))
        output = io.StringIO()
        with patch("miniagent.studio.deepseek_factory",return_value=provider), contextlib.redirect_stdout(output):
            self.assertEqual(main(["--runs-dir",str(runs),"eval","--tasks",str(source)]),0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["summary"]["completed"],6)
        tasks = report["tasks"]
        self.assertEqual(len({r["group_id"] for r in tasks}),3)
        for first, second in zip(tasks[::2],tasks[1::2]):
            self.assertEqual(first["group_id"],second["group_id"])
            self.assertEqual(first["evaluation"]["passed"],1)
            self.assertEqual(second["evaluation"]["passed"],1)
            self.assertEqual(second["evaluation"]["checks"][1]["exit_code"],7)
            self.assertTrue((runs/"groups"/first["group_id"]/"project/marker.txt").read_text().startswith("offline fixture"))

    def test_concurrent_data_dir_writers_are_refused(self):
        with session_lock(self.root), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--runs-dir",str(self.root),"demo"]),1)
        with session_lock(self.root):
            self.assertTrue((self.root/".studio.lock").exists())
