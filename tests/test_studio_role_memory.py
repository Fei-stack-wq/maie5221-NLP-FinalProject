import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from miniagent.studio_role_memory import RoleMemoryConflict, RoleMemoryNotFound, RoleMemoryStore


class RoleMemoryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "knowledge.sqlite3"
        self.store = RoleMemoryStore(self.path, secret="private-fake-key-113")

    def test_active_candidate_rejection_and_historical_restoration(self):
        s = self.store
        first = s.save("ops", kind="skill", title="Disk triage", text="List sizes and verify free space.")
        id = first["id"]
        self.assertEqual(first["active_revision"],1)
        def propose(rev, text):
            return s.save("ops", kind="skill", title="Disk triage", text=text, source="agent",
                          source_run="run-b",status="candidate",entry_id=id,expected_revision=rev)
        draft = propose(1,"In this version check open files first.")
        self.assertEqual(draft["active_revision"],1)
        self.assertEqual(s.get("ops",id)["status"],"candidate")
        self.assertEqual(s.list("ops")[0]["revision"],1)
        self.assertNotIn("open files",s.context("ops"))
        self.assertEqual(s.list("ops",True)[0]["revision"],2)
        rejected = s.set_status("ops",id,"rejected",2)
        self.assertEqual(rejected["active_revision"],1)
        replacement = propose(3,"Measure growth and verify service health.")
        accepted = s.set_status("ops",id,"active",replacement["revision"])
        self.assertEqual(accepted["source_run"],"run-b")
        self.assertEqual(accepted["origin_source"],"user")
        self.assertIn("service health",s.context("ops"))
        self.assertNotIn("free space",s.context("ops"))
        archived = s.set_status("ops",id,"archived",5)
        self.assertIsNone(archived["active_revision"])
        self.assertEqual(s.context("ops"),"")
        reopened = RoleMemoryStore(self.path)
        restored = reopened.restore("ops",id,1,6)
        self.assertEqual((restored["revision"],restored["copied_from"],restored["source"]),(7,1,"user"))
        self.assertIn("free space",reopened.context("ops"))
        self.assertEqual([r["status"] for r in reopened.history("ops",id)],
                         ["active","archived","active","candidate","rejected","candidate","active"])

    def test_unseen_project_and_other_roles_cannot_recall_or_read_foreign_entries(self):
        s = self.store
        s.save("ops",kind="skill",title="日志空间",text="检查日志写入速率")
        a = s.save("ops",kind="memory",title="A 数据",text="A 的采购记录",scope="project",project="proj-a")
        s.save("ops",kind="memory",title="B 需求",text="B 的指标口径",scope="project",project="proj-b")
        s.save("db",kind="memory",text="not ops")
        s.save("ops",kind="memory",text="candidate only",status="candidate")
        self.assertIn("检查日志",s.context("ops","proj-a",query="日志维护"))
        self.assertIn("采购记录",s.context("ops","proj-a"))
        self.assertNotIn("指标口径",s.context("ops","proj-a"))
        self.assertNotIn("采购记录",s.context("ops"))
        self.assertNotIn("candidate only",s.context("ops"))
        self.assertNotIn("not ops",s.context("ops","proj-b"))
        self.assertEqual(s.recall("ops","proj-a","日志维护",limit=1)[0]["kind"],"skill")
        with self.assertRaises(RoleMemoryNotFound):
            s.get("db", a["id"])
        with self.assertRaises(RoleMemoryNotFound):
            s.set_status("db", a["id"], "archived", 1)
        with self.assertRaises(ValueError):
            s.save("ops",kind="memory",text="leak",entry_id=a["id"],expected_revision=1,scope="role")

    def test_concurrent_idempotent_proposal_and_revision_conflict(self):
        s = self.store
        def draft(_):
            return s.save("ops",kind="skill",title="Inspect",text="Check original records.",
                          source="agent",source_run="run-1",status="candidate")
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(draft,range(16)))
        id = results[0]["id"]
        self.assertTrue(all((r["id"],r["revision"]) == (id,1) for r in results))
        def review(state):
            try:
                return s.set_status("ops",id,state,1)["revision"]
            except RoleMemoryConflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(pool.map(review,["active","rejected"]), [2,"conflict"])
        replay = draft(None)
        self.assertEqual(replay["revision"],2)
        self.assertNotEqual(replay["status"],"candidate")
        self.assertEqual(len(s.history("ops",id)),2)
        with self.assertRaises(RoleMemoryConflict):
            s.save("ops",kind="skill",text="lost update",entry_id=id,expected_revision=1)
        with self.assertRaises(ValueError):
            s.save("ops",kind="skill",text="unguarded update",entry_id=id)

    def test_agent_cannot_activate_or_restore_its_never_approved_candidate(self):
        s = self.store
        with self.assertRaises(ValueError):
            s.save("dev",kind="skill",text="only a claim",source="agent",source_run="r")
        with self.assertRaises(ValueError):
            s.save("dev",kind="skill",text="no provenance",source="agent",status="candidate")
        r = s.save("dev",kind="skill",text="to review",source="agent",source_run="r",status="candidate")
        id = r["entry_id"]
        s.set_status("dev",id,"archived",1)
        with self.assertRaises(RoleMemoryConflict):
            s.set_status("dev",id,"active",2)
        with self.assertRaises(RoleMemoryConflict):
            s.restore("dev",id,1,2)
        self.assertEqual(s.list("dev"),[])

    def test_input_bounds_credentials_scope_and_bounded_context(self):
        base = {"kind":"memory","text":"diagnose the files"}
        for edits in [{"kind":"capability"},{"text":" \n"},{"text":"x"*8001},{"title":"y"*161},
                      {"scope":"project"},{"project":"accidental-project"},{"scope":"anywhere"},
                      {"text":"Use sk-"+"a"*40},{"text":"Use private-fake-key-113"},{"source_run":"private-fake-key-113"},
                      {"title":"Authorization: Bearer abcdef123456789xyz"},{"text":"token\0x"}]:
            with self.subTest(edits=str(edits)[:60]), self.assertRaises(ValueError):
                self.store.save("ops",**(base|edits))
        with self.assertRaises(ValueError):
            self.store.save("../ops",**base)
        self.assertEqual(self.store.list("ops",True),[])
        for i in range(20):
            self.store.save("ops",kind="skill",text=f"{i}"+'长描述'*1800,title='规则'*75)
        summary = self.store.context("ops")
        self.assertTrue(0 < len(summary.encode()) <= 6500)
        for marker in (b"private-fake-key-113",b"abcdef123456789xyz",("sk-"+"a"*40).encode()):
            self.assertNotIn(marker,self.path.read_bytes())

    def test_failed_revision_insert_leaves_no_head_or_idempotency_claim(self):
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            db.execute("""CREATE TRIGGER test_failure BEFORE INSERT ON role_revisions
                          BEGIN SELECT RAISE(ABORT,'simulated commit failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.save("dev",kind="skill",text="verify receipt")
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM role_entries").fetchone()[0],0)
            db.execute("DROP TRIGGER test_failure")
        self.assertEqual(self.store.save("dev",kind="skill",text="verify receipt")["revision"],1)
