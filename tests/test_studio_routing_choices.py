"""Verify what the model actually sees and can call from the Studio home conversation."""
import unittest
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.studio_service import StudioError, StudioService
from tests.support import cleanup, make_temp_dir
from tests.test_studio_conversations import RecordingProvider, fresh, msg
from tests.test_studio_service import finished
from tests.test_studio_superagent import message, tool


class StudioRoutingChoicesTest(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir("routing-choices")
        self.service = StudioService(self.root / "runs", Settings(api_key="test-key"))
        self.addCleanup(cleanup, self.root)
        self.addCleanup(self.service.close)

    def assistant(self, payload, script):
        model = RecordingProvider(script)
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=model):
            run = self.service.send_superagent(payload)
            detail = finished(self.service, run["id"])
        self.assertEqual(detail["run"]["status"], "unverified", detail["run"])
        return run["id"], model

    def test_first_model_request_sees_roles_but_only_working_handoff_tools(self):
        self.service.add_profile({"id": "maintainer", "name": "小维", "prompt": "处理开发环境维护。"})
        self.service.role_memory.save("maintainer", kind="memory", text="private-memory-sentinel")
        self.service.role_memory.save("maintainer", kind="skill", title="私有维护方法", text="private-skill-sentinel")
        args = {"target_kind": "role", "target_id": "maintainer", "objective": "检查环境并交付维护清单",
                "input_files": ["requirements.txt"], "acceptance_files": ["check.md"]}
        receipt = {"dispatch_id": "sample", "status": "queued"}
        with patch.object(self.service, "request_studio_dispatch", return_value=receipt, create=True) as dispatch:
            run_id, model = self.assistant(message("请把环境维护工作交给小维"), [
                tool("list_studio_targets", query="维护"),
                tool("dispatch_studio_task", required_participants=["maintainer"], **args), {"text": "小维已接单。"}])
        dispatch.assert_called_once_with(run_id, required_participants=["maintainer"], **args)
        messages, offered = model.requests[0]
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("Superagent", messages[0]["content"])
        self.assertNotIn("请检查项目工作区并按当前用户要求完成交付", str(messages))
        self.assertIn("dispatch_studio_task", offered)
        self.assertIn("list_studio_targets", offered)
        self.assertIn("ask_human", offered)
        self.assertNotIn("delegate_task", offered)
        self.assertIn("处理开发环境维护", str(messages))
        self.assertNotIn("private-memory-sentinel", str(model.requests))
        self.assertNotIn("private-skill-sentinel", str(model.requests))
        self.assertIn("queued", str(model.requests[-1][0]))
        self.assertEqual(self.service.studio_routing_targets(run_id, query="不存在")["targets"], [])

    def test_group_catalog_is_scoped_by_workspace_and_unbound_source_identity(self):
        folder = self.root / "local"
        folder.mkdir()
        roles = {"members": ["researcher", "reviewer"], "lead": "reviewer"}
        def create(name):
            return [tool("create_studio_group", name=name, **roles), {"text": "群聊已创建。"}]
        unbound, _ = self.assistant(message("建一个群"), create("随身小组"))
        other, _ = self.assistant({**message("切换项目并建群", unbound), "workspace_path": str(folder)},
                                  create("本地项目组"))
        def groups(run):
            return [row for row in self.service.studio_routing_targets(run)["targets"] if row["kind"] == "group"]
        self.assertEqual([row["name"] for row in groups(unbound)], ["随身小组"])
        local, = groups(other)
        self.assertEqual((local["name"], local["lead"], local["members"]), ("本地项目组", "reviewer", roles["members"]))
        self.assertEqual(self.service.studio_routing_targets(other, kind="group", query="审阅者")["targets"], [local])
        again, _ = self.assistant(message("本项目有什么团队？", other), [{"text": "本地项目组。"}])
        self.assertEqual(groups(again), [local])
        self.assertIsNone(self.service.studio_routing_group({"thread": {"superagent": True, "id": "a-different-chat"}},
                                                           groups(unbound)[0]["id"]))
        self.assertIsNone(self.service.studio_routing_group(self.service._meta(self.root / "runs" / other),
                                                           "../another-project"))

    def test_candidate_search_reaches_roles_beyond_first_page(self):
        for index in range(18):
            self.service.add_profile({"id": f"role{index}", "name": f"助手{index}", "prompt": f"职责 {index}"})
        run_id, _ = self.assistant(message("只介绍角色"), [{"text": "以上是建议。"}])
        first = self.service.studio_routing_targets(run_id)
        second = self.service.studio_routing_targets(run_id, offset=first["next_offset"])
        listed = [r["id"] for r in first["targets"] + second["targets"]]
        self.assertEqual(len(set(listed)), first["total"])
        self.assertIsNone(second["next_offset"])
        found, = self.service.studio_routing_targets(run_id, query="ROLE17")["targets"]
        self.assertEqual(found["name"], "助手17")
        with self.assertRaises(StudioError):
            self.service.studio_routing_targets(run_id, offset=-1)

    def test_work_requiring_an_independent_participant_must_use_a_group_with_both_roles(self):
        first, _ = self.assistant(message("建一个通知组"), [
            tool("create_studio_group", name="通知组", members=["writer", "reviewer"], lead="writer"), {"text": "已建群。"}])
        group = self.service.studio_routing_targets(first, kind="group")["targets"][0]["id"]
        work = {"objective": "撰写通知，并由另一人对照原记录独立复核。", "acceptance_files": ["notice.md", "review.md"]}
        addressed = {"target_kind": "group", "target_id": group, **work}
        with patch.object(self.service, "request_studio_dispatch", return_value={"status": "queued"}, create=True) as dispatch:
            source, model = self.assistant(message(work["objective"], first), [
                tool("dispatch_studio_task", target_kind="role", target_id="writer",
                     required_participants=["writer", "reviewer"], **work),
                tool("dispatch_studio_task", **addressed, required_participants=["writer", "researcher"]),
                tool("dispatch_studio_task", **addressed, required_participants=["writer", "reviewer"]),
                {"text": "已将整项工作交给通知组。"}])
        dispatch.assert_called_once_with(source, required_participants=["writer", "reviewer"], **addressed)
        self.assertIn("向群负责人派发所有阶段", str(model.requests[1][0]))
        self.assertIn("不包括整项请求所需的所有角色", str(model.requests[2][0]))

    def test_failed_dispatch_can_be_corrected_but_regular_dm_has_no_routing_tools(self):
        args = {"target_kind": "role", "target_id": "writer", "objective": "编写说明"}
        failure = StudioError("目标不存在。", "not_found")
        with patch.object(self.service, "request_studio_dispatch", side_effect=failure, create=True):
            _, model = self.assistant(message("请安排编写说明"), [
                tool("dispatch_studio_task", required_participants=["writer"], **args), {"text": "目标不存在，请提供正确的名称。"}])
        self.assertIn("目标不存在", str(model.requests[-1][0]))
        ordinary = RecordingProvider([{"text": "收到。"}])
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=ordinary):
            run = self.service.start(msg("一般私聊", fresh("writer")))
            finished(self.service, run["id"])
        self.assertFalse({"dispatch_studio_task", "list_studio_targets"} & set(ordinary.requests[0][1]))
        with self.assertRaises(StudioError):
            self.service.studio_routing_targets(run["id"])
