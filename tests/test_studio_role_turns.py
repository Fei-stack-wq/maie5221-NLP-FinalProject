"""Persistent profile identities survive edits, new rooms and Studio restarts."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.studio_service import StudioError, StudioService
from tests.test_studio_conversations import RecordingProvider, fresh, msg, reply
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

    def run_turn(self, payload):
        provider = RecordingProvider([{"text": "已处理。"}])
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
        self.assertIn(user_text, prompt)
        self.assertNotEqual(first, second)
        original = self.studio._meta(self.studio._directory(first))["thread"]["profiles"]
        self.studio.edit_profile("maint", {"previous": {"name":"电脑维护", "prompt":"维护系统"},
                                           "name":"系统维护", "prompt":"优先验证网络设置"})
        self.assertEqual(self.studio.start(first_payload)["id"], first)
        third, prompt = self.run_turn(msg("诊断网络", reply(first, "maint", "question")))
        self.assertIn("优先验证网络设置", prompt)
        self.assertEqual(self.studio._meta(self.studio._directory(first))["thread"]["profiles"], original)
        self.assertEqual(self.studio._meta(self.studio._directory(third))["thread"]["saved_roles"], ["maint"])
        notes = self.studio.role_memory.list("maint")
        self.assertEqual([(row["text"], row["source_run"]) for row in notes], [(user_text, first)])

    def test_private_memory_isolated_and_inline_mismatch_cannot_bind_by_name(self):
        self.studio.role_memory.save("maint", kind="memory", text="private-unique-442")
        other = self.studio.add_profile({"id":"assistantB", "name":"分析", "prompt":"分析数据"})
        _, visible = self.run_turn(msg("读取适用资料", fresh(other["id"], intent="question")))
        self.assertNotIn("private-unique-442", visible)
        fake = {**fresh("maint"), "profiles":[{**self.profile, "prompt":"另一份职责"}]}
        with self.assertRaises(StudioError) as exc:
            self.studio.start(msg("打开资料", fake))
        self.assertEqual(exc.exception.status, 409)
