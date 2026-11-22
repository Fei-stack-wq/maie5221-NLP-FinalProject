"""Persistent profile identities survive edits, new rooms and Studio restarts."""
import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.studio_service import StudioError, StudioService
from miniagent.studio_uploads import save_upload
from tests.test_studio_conversations import RecordingProvider, fresh, msg, reply, tool
from tests.test_studio_service import finished


class RoleTurnsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runs = Path(self.temp.name) / "runs"
        self.settings = Settings(api_key="test-studio-role-key")
        self.studio = StudioService(self.runs, self.settings)
        self.addCleanup(lambda: self.studio.close())
        self.profile = self.studio.add_profile({"id": "maint", "name": "电脑维护", "prompt": "维护系统"})

    def run_turn(self, payload, script=()):
        provider = RecordingProvider([*script, {"text": "已处理。"}])
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider):
            run = self.studio.start(payload)
            detail = finished(self.studio, run["id"])
        self.assertNotEqual(detail["run"]["status"], "failed", detail["run"])
        return run["id"], json.dumps(provider.requests, ensure_ascii=False)

    def test_reopen_separate_dm_then_update_responsibility_without_rewriting_old_turn(self):
        user_text = "网管固定回执 cedar-184"
        first_payload = msg("请记住：" + user_text, fresh("maint", intent="question"))
        first, _ = self.run_turn(first_payload)
        self.studio.close()
        self.studio = StudioService(self.runs, self.settings)
        second, prompt = self.run_turn(msg("按照习惯交付", fresh("maint", intent="question")))
        self.assertNotIn(user_text, prompt)
        self.assertNotEqual(first, second)
        original = self.studio._meta(self.studio._directory(first))["thread"]["profiles"]
        self.studio.edit_profile("maint", {"previous": {"name":"电脑维护", "prompt":"维护系统"},
                                           "name":"系统维护", "prompt":"优先验证网络设置"})
        self.assertEqual(self.studio.start(first_payload)["id"], first)
        third, prompt = self.run_turn(msg("诊断网络", reply(first, "maint", "question")))
        self.assertIn("优先验证网络设置", prompt)
        self.assertIn(user_text, prompt)
        self.assertEqual(self.studio._meta(self.studio._directory(first))["thread"]["profiles"], original)
        self.assertEqual(self.studio._meta(self.studio._directory(third))["thread"]["saved_roles"], ["maint"])
        notes = self.studio.role_memory.list("maint")
        self.assertEqual([(row["text"], row["source_run"]) for row in notes], [(user_text, first)])
        self.assertEqual((notes[0]["scope"], notes[0]["project"]), ("conversation", first))

    def test_same_role_and_folder_isolate_conversation_inputs_and_share_approved_role_skill(self):
        folder = Path(self.temp.name) / "shared"
        folder.mkdir()
        evidence = b"packet-capture-only-in-first"
        old = save_upload(self.runs, "first-case.txt", BytesIO(evidence), len(evidence))
        first, _ = self.run_turn(msg("请记住：本地工单编号 A-9815", fresh("maint"),
                                     workspace_path=str(folder), attachments=[old["id"]]))
        private = self.studio.role_memory.list("maint")[0]
        original = self.studio.role_memory.save("maint", kind="skill", title="网络复测", text="Verify DNS after reboot",
                                                source="agent", source_run=first, status="candidate", scope="role")
        next_a, _ = self.run_turn(msg("整理本工单的检查方法", reply(first, "maint")),
                                  [tool("propose_role_knowledge", kind="skill", title="设备检查", text="probe-A-731")])
        proposed = next(row for row in self.studio.role_memory.list("maint", include_inactive=True)
                        if row["title"] == "设备检查")
        self.assertEqual((proposed["scope"], proposed["project"]), ("conversation", first))
        self.studio.role_operation("maint", "status", {"status": "active", "expected_revision": 1}, proposed["id"])
        own = save_upload(self.runs, "second-case.txt", BytesIO(b"second"), 6)
        probe = [tool("recall_role_knowledge", query="工单"),
                 tool("read_role_knowledge", entry_id=private["id"]), tool("read_attachment", id=old["id"])]
        second, during = self.run_turn(msg("检查我提供的材料", fresh("maint", intent="question"),
                                           attachments=[own["id"]], workspace_path=str(folder)), probe)
        for secret in ("A-9815", "first-case.txt", evidence.decode(), "probe-A-731", "Verify DNS after reboot"):
            self.assertNotIn(secret, during)
        denied = [e for e in self.studio.detail(second)["events"] if e["kind"] == "tool_result"]
        self.assertEqual([(e["tool"], e["ok"]) for e in denied], [
            ("recall_role_knowledge", True), ("read_role_knowledge", False), ("read_attachment", False)])
        self.studio.role_operation("maint", "status", {"status": "active", "expected_revision": 1}, original["id"])
        _, shared = self.run_turn(msg("请复述通用方法", reply(second, "maint", "question")),
                                  [tool("read_role_knowledge", entry_id=original["id"])])
        self.assertIn("Verify DNS after reboot", shared)
        self.assertNotIn("A-9815", shared)
        _, local = self.run_turn(msg("确认原始材料", reply(next_a, "maint", "question")), probe)
        self.assertIn("A-9815", local)
        self.assertIn("probe-A-731", local)
        self.assertIn(evidence.decode(), local)
        self.assertIn("Verify DNS after reboot", local)
        self.assertNotIn("second-case.txt", local)

    def test_restart_migrates_dm_note_from_old_role_scope_with_original_identity_and_revisions(self):
        first, _ = self.run_turn(msg("处理设备", fresh("maint")))
        later, _ = self.run_turn(msg("跟进设备", reply(first, "maint")))
        legacy = self.studio.role_memory.save("maint", kind="memory", text="legacy-private-case-20", source_run=later)
        self.studio.close()
        self.studio = StudioService(self.runs, self.settings)
        default = self.studio.role_memory.get("maint", legacy["id"])
        self.assertEqual((default["scope"], default["project"], default["revision"]), ("conversation", first, 1))
        _, other = self.run_turn(msg("查阅历史", fresh("maint", intent="question")),
                                 [tool("read_role_knowledge", entry_id=legacy["id"])])
        self.assertNotIn("legacy-private-case-20", other)
        _, same = self.run_turn(msg("查阅历史", reply(later, "maint", "question")),
                                [tool("read_role_knowledge", entry_id=legacy["id"])])
        self.assertIn("legacy-private-case-20", same)

    def test_private_memory_isolated_and_inline_mismatch_cannot_bind_by_name(self):
        self.studio.role_memory.save("maint", kind="memory", text="private-unique-442")
        other = self.studio.add_profile({"id":"assistantB", "name":"分析", "prompt":"分析数据"})
        _, visible = self.run_turn(msg("读取适用资料", fresh(other["id"], intent="question")))
        self.assertNotIn("private-unique-442", visible)
        fake = {**fresh("maint"), "profiles":[{**self.profile, "prompt":"另一份职责"}]}
        with self.assertRaises(StudioError) as exc:
            self.studio.start(msg("打开资料", fake))
        self.assertEqual(exc.exception.status, 409)
