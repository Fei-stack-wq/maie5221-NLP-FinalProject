import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.studio_service import StudioError, StudioService
from tests.test_studio_conversations import RecordingProvider, tool
from tests.test_studio_crew import assign, roster, write
from tests.test_studio_service import finished, request


class MemberWorkspaceDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.local = root / "selected"
        self.local.mkdir()
        self.studio = StudioService(root / "runs", Settings(api_key="test-sentinel"))
        self.addCleanup(self.studio.close)

    def run_group(self, steps, **extra):
        provider = RecordingProvider(steps)
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider):
            run = self.studio.start(request("team", objective="work on this project",
                    conversation=roster("lead", "dev", "reviewer"), workspace_path=str(self.local), **extra))
            return finished(self.studio, run["id"])

    def test_two_successive_member_publications_write_before_failed_root_finishes(self):
        observed = []
        record = self.studio._record_room_event
        def capture(run, role, kind, text, **fields):
            if kind == "member_report" and fields.get("published"):
                observed.append((self.studio._meta(self.studio._directory(run))["state"],
                                 (self.local / "notes.md").read_text()))
            return record(run, role, kind, text, **fields)
        with patch.object(self.studio, "_record_room_event", side_effect=capture):
            d = self.run_group([assign("dev","write initial notes"), write("notes.md","first note"), {"text":"delivered"},
                                assign("reviewer","amend previous note"), write("notes.md","complete second note"),
                                {"text":"delivered"}, {"text":"[BLOCKED] unresolved external dependency"}])
        self.assertEqual(observed, [("running","first note"),("running","complete second note")])
        self.assertEqual(d["run"]["status"], "failed")
        self.assertEqual(d["run"]["workspace_member_delivery"]["status"], "published")
        self.assertIsNone(d["run"]["workspace_delivery"])
        path = self.studio._directory(d["run"]["id"])
        self.assertEqual(json.loads((path/"workspace-baseline.json").read_text())["files"], {})
        self.assertEqual(set(json.loads((path/"workspace-projection.json").read_text())["files"]), {"notes.md"})
        reports = [r for r in d["room_events"] if r["kind"] == "member_report"]
        self.assertEqual([r["workspace_delivery"]["files"] for r in reports], [["notes.md"],["notes.md"]])

    def test_user_change_between_handoffs_is_preserved_and_recovery_detects_it(self):
        count = 0
        record = self.studio._record_room_event
        def change(run, role, kind, text, **fields):
            nonlocal count
            if kind == "member_report" and fields.get("published"):
                count += 1
                if count == 1:
                    (self.local / "notes.md").write_text("user edit")
            return record(run, role, kind, text, **fields)
        with patch.object(self.studio, "_record_room_event", side_effect=change):
            d = self.run_group([assign("dev","write"),write("notes.md","first note"),{"text":"ready"},
                                assign("reviewer","update"),write("notes.md","second note"),{"text":"ready"},
                                {"text":"[BLOCKED] unrelated blocker"}])
        self.assertEqual((self.local/"notes.md").read_text(),"user edit")
        self.assertEqual(d["run"]["workspace_member_delivery"]["status"],"failed")
        with self.assertRaises(StudioError):
            self.studio.deliver_public_workspace(d["run"]["id"])
        with self.assertRaisesRegex(StudioError,"本地文件已改变"):
            self.studio.start(request("single", objective="recover", conversation={
                "parent_run_id":d["run"]["id"],"recipient":"lead","intent":"edit","recover_failed":True}))

    def test_old_failed_run_can_replay_public_version_idempotently_without_regrading(self):
        # Older Studio versions wrote only at successful root completion.
        with patch.object(self.studio, "_record_crew_event", self.studio._record_room_event):
            d = self.run_group([assign("dev","write"), write("notes.md","published"),{"text":"ready"},
                                {"text":"[BLOCKED] no final review"}])
        rid = d["run"]["id"]
        self.assertFalse((self.local/"notes.md").exists())
        for _ in range(2):
            receipt = self.studio.deliver_public_workspace(rid)
            self.assertEqual(receipt["files"], ["notes.md"])
            self.assertEqual((self.local/"notes.md").read_text(), "published")
            self.assertEqual(self.studio.detail(rid)["run"]["status"], "failed")
        # Explicit recovery now compares against the latest projected version.
        provider = RecordingProvider([tool("read_file", path="notes.md"), {"text":"used existing delivery"}])
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider):
            follow = self.studio.start(request("single", objective="recover the public version", conversation={
                "parent_run_id":rid, "recipient":"lead","intent":"edit","recover_failed":True}))
            finished(self.studio,follow["id"])
        self.assertIn("published", str(provider.requests[-1][0]))

    def test_private_blocked_draft_cannot_be_exposed_by_public_delivery(self):
        d = self.run_group([assign("dev","draft"),write("draft.md","private"),{"text":"[BLOCKED] still open"},
                            {"text":"[BLOCKED] no delivery"}])
        self.assertFalse((self.local/"draft.md").exists())
        with self.assertRaisesRegex(StudioError, "没有已发布"):
            self.studio.deliver_public_workspace(d["run"]["id"])

    def test_unmodified_final_turn_keeps_member_files_in_final_receipt(self):
        d = self.run_group([assign("dev","write"),write("notes.md","finished artifact text"),{"text":"ready"},
                            {"text":"complete"}],acceptance_files=["notes.md"])
        self.assertEqual(d["run"]["status"], "completed")
        self.assertEqual(d["run"]["workspace_delivery"], {"status":"published","files":["notes.md"]})
        self.assertEqual(d["conversation"]["turns"][-1]["artifacts"][0]["name"], "notes.md")

    def run_with_external_edit_after_first_member(self, edit):
        original = self.studio._record_room_event
        def record(run, role, kind, text, **fields):
            if kind == "member_report" and role == "dev" and fields.get("published"):
                edit.write_text("user's own revision")
            return original(run, role, kind, text, **fields)
        with patch.object(self.studio, "_record_room_event", side_effect=record):
            return self.run_group([
                assign("dev", "write notes"), write("notes.md","development handoff version"), {"text":"notes handed off"},
                assign("reviewer","publish QA"), write("qa.md","quality assurance version"), {"text":"QA handed off"},
                {"text":"finished"},
            ], acceptance_files=["notes.md","qa.md"])

    def test_final_rechecks_earlier_projected_file_even_with_no_private_delta(self):
        result = self.run_with_external_edit_after_first_member(self.local/"notes.md")
        self.assertEqual(result["run"]["status"], "failed")
        self.assertEqual(result["run"]["workspace_delivery"]["status"], "failed")
        self.assertIn("notes.md", result["run"]["workspace_delivery"]["message"])
        self.assertEqual((self.local/"notes.md").read_text(), "user's own revision")
        self.assertEqual((self.local/"qa.md").read_text(), "quality assurance version")
        reports = [r for r in result["room_events"] if r["kind"]=="member_report"]
        self.assertEqual([r["workspace_delivery"]["status"] for r in reports], ["published","published"])
        first_link = reports[0]["artifacts"][0]["path"]
        self.assertEqual(self.studio.download_artifact(result["run"]["id"],first_link), b"development handoff version")

    def test_unrelated_preexisting_local_edit_is_preserved_and_does_not_block_final(self):
        untouched = self.local / "personal.md"
        untouched.write_text("before")
        result = self.run_with_external_edit_after_first_member(untouched)
        self.assertEqual(result["run"]["status"], "completed", result["run"])
        self.assertEqual(result["run"]["workspace_delivery"]["files"], ["notes.md","qa.md"])
        self.assertEqual(untouched.read_text(), "user's own revision")

    def test_manual_delivery_rejects_changed_untargeted_local_file_and_broken_basis(self):
        (self.local/"existing.md").write_text("original")
        with patch.object(self.studio, "_record_crew_event", self.studio._record_room_event):
            d = self.run_group([assign("dev","write"),write("notes.md","published"),{"text":"ready"},
                                {"text":"[BLOCKED] still missing review"}])
        rid = d["run"]["id"]
        (self.local/"existing.md").write_text("user edition")
        with self.assertRaisesRegex(StudioError, "版本不符"):
            self.studio.deliver_public_workspace(rid)
        self.assertFalse((self.local/"notes.md").exists())
        self.assertEqual((self.local/"existing.md").read_text(), "user edition")
        (self.local/"existing.md").write_text("original")
        (self.studio._directory(rid)/"workspace-projection.json").write_bytes(b"")
        with self.assertRaisesRegex(StudioError, "清单无效"):
            self.studio.deliver_public_workspace(rid)
        self.assertFalse((self.local/"notes.md").exists())
