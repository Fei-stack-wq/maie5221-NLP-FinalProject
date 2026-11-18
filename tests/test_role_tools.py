import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from miniagent.role_tools import ROLE_READ_TOOLS, ROLE_TOOLS, attach_role_tools
from miniagent.studio_role_memory import RoleMemoryStore
from miniagent.tools import ToolRegistry, build_default_registry


class RoleToolTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = RoleMemoryStore(self.root / "roles.sqlite3")
        self.reg = self.bind()
        self.shared = self.store.save("dev", kind="memory", title="回复语言", text="使用中文写维护说明")
        self.project = self.store.save("dev", scope="project", project="a", kind="skill", title="预约回归",
                                       text="准备独立副本。\n"+("核实用例。"*120)+"\n最后检查全天边界。")
        self.other = self.store.save("dev", scope="project", project="b", kind="skill", title="其他客户",
                                     text="other-project-file")
        self.qa = self.store.save("qa", kind="memory", title="质量组", text="qa-private-note")

    def bind(self, role="dev", project="a", run="run1", base=None, **options):
        return attach_role_tools(base or ToolRegistry(), self.store, role, project, run, **options)

    def run_tool(self, name, args, reg=None):
        return (reg or self.reg).dispatch(name, args, None)

    def proposal(self, **args):
        return self.run_tool("propose_role_knowledge", args)

    def read(self, entry_id, **args):
        return self.run_tool("read_role_knowledge", {"entry_id":entry_id, **args})

    def test_catalog_then_load_full_approved_skill(self):
        recall = self.run_tool("recall_role_knowledge", {"query":"预约", "kind":"skill"})
        self.assertTrue(recall.ok)
        entries = json.loads(recall.content)
        self.assertEqual([e["entry_id"] for e in entries], [self.project["id"]])
        self.assertLess(len(entries[0]["excerpt"]), len(self.project["text"]))
        skill = self.read(entries[0]["entry_id"], revision=entries[0]["revision"])
        self.assertTrue(skill.ok)
        self.assertEqual(json.loads(skill.content.split("\n",1)[1])["text"], self.project["text"])
        self.assertEqual(self.read(self.shared["id"]).meta["kind"], "memory")
        self.assertFalse(self.read(self.project["id"], revision=42).ok)

    def test_unknown_private_or_cross_scope_ids_have_identical_response(self):
        for row in [self.qa, self.other, {"id":"nonexistent"}]:
            result = self.read(row["id"])
            self.assertFalse(result.ok)
            self.assertEqual(result.content, self.read("not-real").content)
            proposal = self.proposal(kind="skill", text="replace", entry_id=row["id"], base_revision=1)
            self.assertFalse(proposal.ok)
            self.assertNotIn("other-project-file", proposal.content)
        recalled = self.run_tool("recall_role_knowledge", {}, self.bind(project=""))
        self.assertEqual([e["entry_id"] for e in json.loads(recalled.content)], [self.shared["id"]])

    def test_candidate_created_once_bound_to_role_run_scope_and_user_decides(self):
        args = {"kind":"memory", "text":"在副本上运行浏览器验证"}
        first = self.proposal(**args)
        duplicate = self.proposal(**args)
        self.assertEqual(first.meta, duplicate.meta)
        self.assertEqual(first.meta["status"], "candidate")
        entry = self.store.get("dev", first.meta["entry_id"])
        self.assertEqual((entry["project"], entry["source"], entry["source_run"]), ("a","agent","run1"))
        self.assertFalse(self.read(entry["id"]).ok)
        self.assertNotIn(entry["id"], self.run_tool("recall_role_knowledge", {}).content)
        accepted = self.store.set_status("dev", entry["id"], "active", entry["revision"])
        new_run = self.bind(run="next-run")
        loaded = self.run_tool("read_role_knowledge", {"entry_id":entry["id"]}, new_run)
        self.assertTrue(loaded.ok)
        self.assertEqual(loaded.meta["revision"], accepted["revision"])
        self.assertEqual(len(self.store.history("dev", entry["id"])), 2)

    def test_amendment_keeps_active_copy_and_stale_or_concurrent_edits_conflict(self):
        entry = self.project
        args = {"kind":"skill", "text":"补充午夜浏览器测试", "entry_id":entry["id"],
                "base_revision":entry["revision"]}
        candidate = self.proposal(**args)
        self.assertTrue(candidate.ok, candidate.content)
        self.assertEqual(self.proposal(**args).meta, candidate.meta)
        self.assertFalse(self.proposal(**{**args, "text":"different while pending"}).ok)
        self.assertEqual(self.read(entry["id"]).meta["revision"], entry["revision"])
        self.assertNotIn(args["text"], self.run_tool("recall_role_knowledge", {}).content)
        self.assertFalse(self.read(entry["id"], revision=candidate.meta["revision"]).ok)
        self.store.set_status("dev", entry["id"], "rejected", candidate.meta["revision"])
        amended = self.proposal(**{**args, "text":"新方法，附验证结果"})
        self.assertTrue(amended.ok)
        self.store.set_status("dev", entry["id"], "active", amended.meta["revision"])
        approved = self.read(entry["id"])
        self.assertIn("新方法，附验证结果", approved.content)
        self.assertEqual(approved.meta["title"], entry["title"])
        self.assertFalse(self.proposal(**args).ok)

    def test_midflight_decision_has_atomic_conflict(self):
        row = self.shared
        save = self.store.save
        def change_then_save(role, **options):
            save("dev", kind="memory", entry_id=row["id"], text="用户自己的新版本",
                 expected_revision=row["revision"])
            return save(role, **options)
        with patch.object(self.store, "save", side_effect=change_then_save):
            result = self.proposal(kind="memory", text="stale change", entry_id=row["id"],
                                   base_revision=row["revision"])
        self.assertFalse(result.ok)
        self.assertIn("用户自己的新版本", self.read(row["id"]).content)

    def test_untrusted_overrides_and_sensitive_material_blocked_before_storage(self):
        before = self.store.list("dev", include_inactive=True)
        args = {"kind":"memory", "text":"short fact"}
        for key,value in [("role","qa"),("run_id","fake"),("source","user"),("source_run","other"),
                          ("status","active"),("project","b"),("expected_revision",1)]:
            self.assertFalse(self.proposal(**args, **{key:value}).ok, key)
        for update in [{"kind":"unknown"}, {"kind":"skill", "title":""}, {"scope":"other"},
                       {"title":"too long"*50}, {"text":" "}, {"text":"a"*8001}, {"entry_id":self.shared["id"]},
                       {"base_revision":1}, {"text":"sk-"+"Q"*40}]:
            self.assertFalse(self.proposal(**{**args, **update}).ok)
        secret = "test-only-secret"
        for reg in [self.bind(secret=secret), self.bind(redactor=lambda value:value.replace(secret,"***"))]:
            denied = self.run_tool("propose_role_knowledge", {**args,"text":"never record "+secret}, reg)
            self.assertFalse(denied.ok)
            self.assertNotIn(secret, denied.content)
        self.assertEqual(self.store.list("dev", include_inactive=True), before)
        self.assertFalse(self.run_tool("propose_role_knowledge", args, self.bind(run="")).ok)

    def test_read_only_and_independent_bound_registries(self):
        bare = build_default_registry(read_only=True)
        worker = self.bind(base=bare)
        qa = self.bind(role="qa", base=worker, read_only=True)
        self.assertFalse(ROLE_TOOLS.intersection(bare.names()))
        self.assertEqual(ROLE_TOOLS.intersection(qa.names()), ROLE_READ_TOOLS)
        self.assertTrue(ROLE_TOOLS.issubset(worker.names()))
        self.assertFalse(self.run_tool("propose_role_knowledge", {"kind":"memory","text":"no"}, qa).ok)
        self.assertIn("qa-private-note", self.run_tool("read_role_knowledge", {"entry_id":self.qa["id"]}, qa).content)
        self.assertFalse(self.run_tool("read_role_knowledge", {"entry_id":self.qa["id"]}, worker).ok)
        self.assertTrue(bare.get("read_file") is worker.get("read_file"))
