import json
import unittest
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.studio_service import StudioService
from tests.support import make_temp_dir, cleanup
from tests.test_studio_conversations import RecordingProvider, msg, reply
from tests.test_studio_service import finished
from tests.test_studio_superagent import message, tool


class RoleBriefScopeTests(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir("studio-role-briefs")
        self.settings = Settings(api_key="test-key")
        self.service = StudioService(self.root, self.settings)
        self.profile = {"id": "qa", "name": "质量工程师", "prompt": "Task-A-only acceptance criteria xyz"}
        self.shared = {"id": "curator", "name": "编辑", "prompt": "Reusable human-authored duties"}
        self.service.add_profile(self.shared)
        roster = {"lead": "qa", "members": ["qa", "curator"]}
        model = RecordingProvider([tool("create_studio_role", **self.profile),
                                   tool("create_studio_group", name="Alpha", **roster),
                                   tool("create_studio_group", name="Beta", **roster), {"text": "已建群。"}])
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=model):
            self.creator = self.service.send_superagent(message("创建角色，为两个群工作。"))["id"]
            events = finished(self.service, self.creator)["room_events"]
        self.a, self.b = [e["entity"]["id"] for e in events if e.get("entity", {}).get("kind") == "group"]

    def tearDown(self):
        self.service.close()
        cleanup(self.root)

    def model_view(self, parent):
        model = RecordingProvider([{"text": "处理完毕。"}])
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=model):
            run = self.service.start(msg("按本群要求执行", reply(parent, "qa")))
            finished(self.service, run["id"])
        return str(model.requests[0]), run["id"]

    def test_task_brief_visible_in_original_group_and_role_editor_can_publish(self):
        a, _ = self.model_view(self.a)
        b, next_b = self.model_view(self.b)
        self.assertNotIn(self.profile["prompt"], (self.root / self.b / "studio.json").read_text())
        self.service.update_members(self.b, {"members": ["qa", "curator", "worker"], "lead": "qa", "revision": 0})
        self.assertNotIn(self.profile["prompt"], (self.root / self.b / "studio-members.json").read_text())
        self.assertIn(self.profile["prompt"], a)
        self.assertNotIn(self.profile["prompt"], b)
        self.assertIn(self.shared["prompt"], b)
        self.assertIn(self.profile["name"], b)
        directory_query = RecordingProvider([tool("list_studio_roles"), tool("list_studio_targets", kind="role"),
                                             {"text": "已列出。"}])
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=directory_query):
            source = self.service.send_superagent(message("查找角色", self.creator))["id"]
            finished(self.service, source)
        self.assertNotIn(self.profile["prompt"], str(directory_query.requests))
        self.assertIn(self.shared["prompt"], str(directory_query.requests))
        fields = {key: self.profile[key] for key in ("name", "prompt")}
        renamed = {**fields, "name": "第二位质量工程师"}
        self.service.edit_profile("qa", {**renamed, "previous": fields})
        b, next_b = self.model_view(next_b)
        self.assertNotIn(self.profile["prompt"], b)
        self.assertIn(renamed["name"], b)
        scoped = {**renamed, "brief_scope": "source"}
        self.service.edit_profile("qa", {**renamed, "share_brief": True, "previous": scoped})
        self.assertIn(self.profile["prompt"], self.model_view(next_b)[0])
        self.assertEqual(self.service.role_library("qa")["brief_scope"], "role")

    def test_legacy_role_card_migration_matches_verified_creation_arguments(self):
        self.service.close()
        # Older versions persisted only the public form fields.
        (self.root / "studio-profiles.json").write_text(json.dumps([self.profile, self.shared]))
        self.service = StudioService(self.root, self.settings)
        self.assertIn(self.profile["prompt"], self.model_view(self.a)[0])
        self.assertNotIn(self.profile["prompt"], self.model_view(self.b)[0])
        # A user's subsequent edit differs from the original recorded arguments.
        self.service.close()
        revised = {**self.profile, "prompt": "Personally curated testing responsibilities"}
        (self.root / "studio-profiles.json").write_text(json.dumps([revised, self.shared]))
        self.service = StudioService(self.root, self.settings)
        self.assertTrue(all(revised["prompt"] in str(self.service.detail(room)["conversation"]["profiles"])
                            for room in (self.a, self.b)))
