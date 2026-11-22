"""A returning deliverable may supersede its own unchanged private import."""
import json
import unittest

from tests import test_studio_route_results as support
from tests.test_studio_superagent import tool


class RoutedRevisionTest(unittest.TestCase):
    setUp = support.ReturnedFilesTest.setUp
    routed = support.ReturnedFilesTest.routed

    def produce(self, version, *, parent=None, role="worker", workspace=None, prior_ref=None):
        options = {"continue_dispatch_id": prior_ref.split("/")[0].removeprefix("dispatch:")} if prior_ref else {}
        parent, row, _ = self.routed(
            [support.dispatch(role, "更新报告 " + version, **options), {"text": "已派单。"}],
            [tool("write_file", path="report.md", content=version), {"text": "报告已交付。"}],
            parent=parent, workdir=workspace)
        self.assertEqual(row["status"], "unverified", row)
        return parent, row["artifacts"][0]["ref"]

    def review(self, parent, ref, *, edit=None, prior=None):
        options = {"continue_dispatch_id": prior["dispatch_id"]} if prior else {}
        steps = [tool("read_file", path="report.md")]
        if edit:
            steps.append(tool("write_file", path="report.md", content=edit))
        return self.routed(
            [support.dispatch("reviewer", "复核收到的报告", input_files=[ref], **options), {"text": "已派单。"}],
            [*steps, {"text": "已复核。"}], parent=parent)

    def test_four_turn_revision_reuses_reviewer_and_keeps_prior_file_frozen(self):
        parent, v1 = self.produce("第一版正文")
        parent, initial_review, _ = self.review(parent, v1)
        self.assertEqual(initial_review["status"], "unverified")
        parent, v2 = self.produce("第二版正文", parent=parent, prior_ref=v1)
        parent, revised, providers = self.review(parent, v2, prior=initial_review)
        self.assertEqual(revised["status"], "unverified", revised)
        self.assertEqual(revised["conversation_id"], initial_review["conversation_id"])
        self.assertIn("第二版正文", json.dumps(providers[1].requests[-1][0], ensure_ascii=False))
        self.assertEqual(revised["artifacts"], [])  # received input isn't reviewer-authored output
        initial = self.service.runs_dir / initial_review["child_run_id"] / "agents/reviewer/report.md"
        self.assertEqual(initial.read_text(), "第一版正文")
        _, rollback, _ = self.review(parent, v1, prior=revised)
        self.assertEqual(rollback["status"], "failed")

    def test_edited_or_different_authors_input_cannot_overwrite(self):
        parent, v1 = self.produce("原作者第一版")
        parent, reviewed, _ = self.review(parent, v1, edit="评审员自己的修改")
        self.assertEqual(reviewed["status"], "unverified")
        parent, v2 = self.produce("原作者第二版", parent=parent, prior_ref=v1)
        parent, refused, _ = self.review(parent, v2, prior=reviewed)
        self.assertEqual(refused["status"], "failed")
        self.assertIn("同名", refused["error"])
        self.assertIsNone(refused["child_run_id"])

        # A fresh recipient now has an original, unchanged import.
        parent, imported, _ = self.routed(
            [support.dispatch("researcher", "核对原件", input_files=[v2]), {"text": "已派单。"}],
            [tool("read_file", path="report.md"), {"text": "已读。"}], parent=parent)
        self.assertEqual(imported["status"], "unverified")
        parent, other = self.produce("另一位作者的同名文件", parent=parent, role="writer")
        _, refused, _ = self.routed(
            [support.dispatch("researcher", "核对其他作者", input_files=[other],
                              continue_dispatch_id=imported["dispatch_id"]), {"text": "已派单。"}],
            [], parent=parent)
        self.assertEqual(refused["status"], "failed")
        self.assertIn("同名", refused["error"])

    def test_bound_workspace_keeps_user_changes(self):
        workspace = self.root / "project"
        workspace.mkdir()
        parent, v1 = self.produce("已交付的原版", workspace=workspace)
        parent, reviewed, _ = self.review(parent, v1)
        self.assertEqual(reviewed["status"], "unverified")
        parent, v2 = self.produce("已交付的新版", parent=parent, prior_ref=v1)
        (workspace / "report.md").write_text("用户本地修改")
        _, refused, _ = self.review(parent, v2, prior=reviewed)
        self.assertEqual(refused["status"], "failed")
        self.assertEqual((workspace / "report.md").read_text(), "用户本地修改")
