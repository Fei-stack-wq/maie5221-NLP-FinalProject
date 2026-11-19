"""Addressing a message picks real responders inside the existing room."""
import hashlib
import uuid
import unittest
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.studio_conversations import addressed_members
from miniagent.studio_reviews import digest
from miniagent.studio_service import StudioError, StudioService
from tests.support import cleanup, make_temp_dir
from tests.test_studio_conversations import RecordingProvider, fresh, reply, tool
from tests.test_studio_service import finished, request


class GroupAddressingTest(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir("addressed")
        self.service = StudioService(self.root / "runs", Settings(api_key="test-key"))
        self.addCleanup(cleanup, self.root)
        self.addCleanup(self.service.close)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.anchor = self.service.create_channel({
            "name": "写作组", "members": ["researcher", "writer", "reviewer"], "lead": "researcher",
            "workspace_path": str(self.workspace), "request_id": str(uuid.uuid4())})["id"]

    def execute(self, text, steps, *, mode="team", intent="edit", **kwargs):
        provider = RecordingProvider(steps)
        parent = self.service.detail(self.anchor)["conversation"]["head_run_id"]
        data = request(mode, objective=text, conversation=reply(parent, "researcher", intent), **kwargs)
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider):
            run = self.service.start(data)
            result = finished(self.service, run["id"])
            self.assertEqual(self.service.start(data)["id"], run["id"])
        return result, provider.requests

    def test_direct_member_and_lead_edit_the_selected_workspace_without_any_child_task(self):
        for role, prefix in (("writer", "@写作者 "), ("researcher", "研究员，")):
            with self.subTest(role=role):
                note = f"Complete inventory notes prepared independently by {role}."
                result, calls = self.execute(prefix + "写出笔记，正文保留 @reviewer 这个记号。", [
                    tool("write_file", path="notes.md", content=note),
                    {"text":"已完成。"}], acceptance_files=["notes.md"])
                self.assertEqual(result["run"]["status"], "completed", result["diagnostics"])
                self.assertEqual([(t["assignee_profile_id"], t["parent_id"]) for t in result["tasks"]], [(role,None)])
                self.assertEqual(result["conversation"]["id"], self.anchor)
                self.assertEqual(result["conversation"]["lead"], "researcher")
                self.assertEqual((self.workspace / "notes.md").read_text(), note)
                self.assertIn("write_file", calls[0][1])
                self.assertFalse(any(e["kind"] == "assignment" for e in result["room_events"]))
        answer, calls = self.execute("@审阅者 谁写了笔记？", [
            tool("read_file", path="notes.md"), {"text":"研究员。"}], mode="single", intent="question")
        self.assertEqual(answer["tasks"][0]["assignee_profile_id"], "reviewer")
        self.assertNotIn("write_file", calls[0][1])

    def test_address_validation_never_silently_substitutes_another_member(self):
        with self.assertRaises(StudioError) as error:
            self.execute("@absent 请写资料", [])
        self.assertEqual(error.exception.code, "invalid_addressee")
        thread = fresh("alpha", members=["alpha","beta"])
        thread["profiles"] = [dict(id=role, name="Editor One", prompt="write") for role in thread["members"]]
        with self.assertRaisesRegex(ValueError, "alpha、beta"):
            addressed_members("@Editor One please write", thread)
        self.assertEqual(addressed_members("@alpha、@beta，check the @alpha example", thread), ["alpha","beta"])
        self.assertEqual(addressed_members("Compare names like @alpha in documentation", thread), [])

    def test_first_address_keeps_room_lead_and_a_one_person_room_needs_no_self_delegation(self):
        data = request("team", objective="@writer 请先给个初稿思路。",
                       conversation=fresh("researcher", members=["researcher", "writer"]))
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=RecordingProvider([{"text": "从问题背景写起。"}])):
            result = finished(self.service, self.service.start(data)["id"])
        self.assertEqual(result["conversation"]["lead"], "researcher")
        self.assertEqual([task["assignee_profile_id"] for task in result["tasks"]], ["writer"])

        self.anchor = self.service.create_channel({"name": "个人研究角", "members": ["researcher"],
                      "lead": "researcher", "request_id": str(uuid.uuid4())})["id"]
        result, _ = self.execute("请给出本地日志的排查思路。", [{"text": "先核对系统日志的时间和错误等级。"}])
        self.assertEqual(result["run"]["status"], "unverified")
        self.assertEqual([task["assignee_profile_id"] for task in result["tasks"]], ["researcher"])
        with self.assertRaisesRegex(StudioError, "两位不同成员"):
            self.execute("请独立核对来源。", [], required_source_review_paths=["guide.md"])

    def test_later_turn_can_independently_review_a_direct_publication_without_an_artificial_author_task(self):
        note = "The workshop inventory lists four desks and two cabinets."
        sha = hashlib.sha256(note.encode()).hexdigest()
        written, _ = self.execute("@writer 写出库存。", [
            tool("write_file", path="inventory.md", content=note), {"text": "已写好库存。"}])
        self.assertEqual(written["run"]["status"], "unverified")
        published, = [e for e in written["room_events"] if e["kind"] == "direct_delivery"]
        self.assertEqual(published["artifacts"][0]["sha256"], sha)
        def review(role):
            return tool("delegate_task", assignee=role, objective="Inspect the original inventory.",
                        remaining_roles=[], review={"candidate": "inventory.md", "criteria": [
                            {"id": "count", "method": "inspect", "description": "Verify the desk count."}]})
        checked, calls = self.execute("独立核对上轮的库存。", [
            review("writer"), review("writer"), review("reviewer"), tool("read_file", path="inventory.md"),
            tool("submit_review", candidate_version=digest({"inventory.md": sha}), results=[
                {"criterion_id": "count", "verdict": "passed", "reason": "The inventory lists four desks.",
                 "evidence": ["file:inventory.md"]}]),
            {"text": "核对完成。"}, {"text": "已提交独立核对结果。"}], acceptance_files=["inventory.md"])
        self.assertEqual(checked["run"]["status"], "completed", checked["diagnostics"])
        bound, = [e for e in checked["room_events"] if e["kind"] == "published_review_bound"]
        self.assertEqual(bound["publication"], {"role": "writer", "files": {"inventory.md": sha},
                         "run_id": written["run"]["id"], "task_id": written["run"]["id"]})
        self.assertEqual(bound["reviewer"], "reviewer")
        self.assertIn("原作者不能独立验收", str(calls))

    def test_direct_writer_can_recruit_independent_reviewer_for_its_actual_bytes(self):
        article = "The room has four desks, according to inventory record 075."
        candidate = digest({"guide.md":hashlib.sha256(article.encode()).hexdigest()})
        review = {"candidate":"guide.md","criteria":[{"id":"facts","method":"inspect","description":"Check the four desks."}]}
        result, _ = self.execute("@writer 写清单并在有草稿后独立核对。", [
            tool("write_file",path="guide.md",content=article),
            tool("delegate_task",assignee="reviewer",objective="Independently inspect the inventory count.",
                 remaining_roles=[],review=review),
            tool("read_file",path="guide.md"),
            tool("submit_review",candidate_version=candidate,results=[{
                "criterion_id":"facts","verdict":"passed","evidence":["file:guide.md"],"reason":"The text names four desks."}]),
            {"text":"已独立核对。"}, {"text":"已完成并核对。"}], acceptance_files=["guide.md"])
        self.assertEqual(result["run"]["status"], "completed", result["diagnostics"])
        reports = [e for e in result["room_events"] if e["kind"] == "member_report"]
        self.assertEqual([r["role"] for r in reports], ["reviewer"])
        check = reports[0]["handoff"]["native_review"]
        self.assertEqual((check["author"], check["reviewer"], check["candidate_version"]), ("writer","reviewer",candidate))

    def test_multiple_read_only_addressees_both_give_evidence_and_cannot_edit(self):
        (self.workspace / "data.md").write_text("Initial inventory")
        def ask(role):
            return tool("delegate_task",assignee=role,objective="Read the inventory and give your assessment.",
                        remaining_roles=[])
        result, calls = self.execute("@writer @reviewer 请分别看文件然后给意见。", [
            ask("writer"), tool("read_file",path="data.md"), {"text":"写作者意见。"},
            {"text":"只收到了写作者意见，尝试结束。"},
            ask("reviewer"), tool("read_file",path="data.md"), {"text":"复核人意见。"}, {"text":"两人均给出意见。"}
        ], mode="single", intent="question")
        self.assertEqual(result["run"]["status"], "unverified", result["diagnostics"])
        self.assertEqual([e["role"] for e in result["room_events"] if e["kind"] == "member_report"],
                         ["writer","reviewer"])
        self.assertTrue(all("write_file" not in names and "run_shell" not in names for _,names in calls))
        self.assertEqual(result["artifacts"], [])
        self.assertEqual((self.workspace / "data.md").read_text(), "Initial inventory")

    def test_one_addressed_reader_can_optionally_consult_another_reader_in_the_same_room(self):
        note = "Inventory version 12 lists 80 parts."
        (self.workspace / "data.md").write_text(note)
        result, calls = self.execute("@writer 请判断库存；需要时请同事看一眼。", [
            tool("read_file", path="data.md"),
            tool("delegate_task", assignee="reviewer", objective="Check the version and count in data.md.",
                 remaining_roles=[]),
            tool("read_file", path="data.md"), {"text": "已读到第 12 版的 80 件库存。"},
            {"text": "我与审阅者都看到第 12 版有 80 件。"}], intent="question")
        self.assertEqual(result["run"]["status"], "unverified", result["diagnostics"])
        root, child = sorted(result["tasks"], key=lambda task: task["parent_id"] is not None)
        self.assertEqual((root["assignee_profile_id"], child["assignee_profile_id"], child["parent_id"]),
                         ("writer", "reviewer", root["task_id"]))
        self.assertTrue(all(not {"write_file", "edit_file", "run_shell"}.intersection(names) for _, names in calls))
        self.assertEqual([e["role"] for e in result["room_events"] if e["kind"] == "member_report"], ["reviewer"])
        self.assertFalse(any(e["kind"] == "direct_delivery" for e in result["room_events"]))
        self.assertEqual(result["artifacts"], [])
        self.assertEqual((self.workspace / "data.md").read_text(), note)
