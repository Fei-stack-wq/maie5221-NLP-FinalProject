import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.studio_service import StudioError, StudioService
from miniagent.studio_workspace import checkout, publish, WorkspaceError
from tests.test_studio_conversations import RecordingProvider, tool
from tests.test_studio_service import request, finished


class LocalWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.local = root / "my project"
        self.local.mkdir()
        self.private = root / "private"
        self.svc = StudioService(root / "runs", Settings(api_key="fake-service-key"))
        self.addCleanup(self.svc.close)

    def test_files_conflicts_and_concurrent_edits(self):
        for name in ["a.md", "b.md", "unrelated.md", ".hidden.md", "ignore.bin"]:
            (self.local / name).write_text(name)
        baseline = checkout(self.local, self.private, {".md"}, "")
        self.assertEqual(set(baseline["files"]), {"a.md", "b.md", "unrelated.md"})
        (self.private / "a.md").write_text("agent A")
        (self.private / "b.md").unlink()
        (self.private / "docs").mkdir()
        (self.private / "docs/a.md").write_text("new")
        (self.local / "b.md").write_text("human revision")
        with self.assertRaises(WorkspaceError):
            publish(self.local, self.private, baseline, {".md"}, "")
        self.assertEqual((self.local / "a.md").read_text(), "a.md")
        self.assertFalse((self.local / "docs").exists())
        (self.local / "b.md").write_text("b.md")
        (self.local / "unrelated.md").write_text("edited in GUI")
        self.assertEqual(publish(self.local, self.private, baseline, {".md"}, ""), ["a.md", "b.md", "docs/a.md"])
        self.assertEqual((self.local / "unrelated.md").read_text(), "edited in GUI")
        self.assertFalse((self.local / "b.md").exists())
        self.assertEqual((self.local / "docs/a.md").read_text(), "new")

    def test_symlink_to_other_directory_and_replaced_workspace_fail_closed(self):
        baseline = checkout(self.local, self.private, {".md"}, "")
        (self.private / "sub").mkdir()
        (self.private / "sub/file.md").write_text("new")
        outside = self.local.parent / "outside"
        outside.mkdir()
        (self.local / "sub").symlink_to(outside)
        with self.assertRaises((OSError, WorkspaceError)):
            publish(self.local, self.private, baseline, {".md"}, "")
        self.assertFalse((outside / "file.md").exists())
        self.local.rename(self.local.with_name("old"))
        self.local.mkdir()
        with self.assertRaises(WorkspaceError):
            publish(self.local, self.private, baseline, {".md"}, "")

    def test_chat_links_preserve_versions_follow_local_edits_and_questions_are_read_only(self):
        first = self.svc.start(request(workspace_path=str(self.local)))
        detail = finished(self.svc, first["id"])
        self.assertEqual(detail["run"]["status"], "completed")
        filename = detail["conversation"]["turns"][0]["artifacts"][0]["path"]
        self.assertEqual((self.local / "notes.md").read_bytes(), self.svc.download_artifact(first["id"], filename))
        (self.local / "notes.md").write_text("edited locally")
        provider = RecordingProvider([tool("read_file", path="notes.md"), {"text": "Reviewed"}])
        def submit(parent, intent, provider, **extra):
            with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider):
                run = self.svc.start(request("single", objective="read this", conversation={
                    "parent_run_id": parent, "intent": intent, "recipient": "worker"}, **extra))
                return finished(self.svc, run["id"])
        question = submit(first["id"], "question", provider)
        self.assertIn("edited locally", str(provider.requests[-1][0]))
        self.assertEqual(question["conversation"]["turns"][-1]["artifacts"], [])
        self.assertNotIn(b"edited locally", self.svc.download_artifact(first["id"], filename))
        update = RecordingProvider([tool("write_file", path="new.md", content="second turn, with a new project document"), {"text": "done"}])
        second = submit(question["run"]["id"], "edit", update, acceptance_files=["new.md"])
        self.assertEqual(second["run"]["status"], "completed", (second["run"], second["acceptance"]))

        self.assertEqual(second["conversation"]["workspace_path"], str(self.local))
        self.assertEqual([a["name"] for a in second["artifacts"]], ["new.md"])
        self.assertIn("notes.md", [a["name"] for a in second["other_project_files"]])
        self.assertEqual([a["name"] for a in second["conversation"]["turns"][-1]["artifacts"]], ["new.md"])
        self.assertEqual((self.local / "new.md").read_text(), "second turn, with a new project document")
        self.assertEqual((self.local / "notes.md").read_text(), "edited locally")

    def test_explicit_recovery_uses_validated_member_checkpoint_when_public_project_empty(self):
        from tests.test_studio_crew import roster, assign, write
        first = RecordingProvider([assign("author", "make a draft"), write("records.json", '[{"room":"A"}]'),
                                   {"text": "[BLOCKED] needs independent test"}, {"text": "[BLOCKED] incomplete"}])
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=first):
            run = self.svc.start(request("team", objective="create", conversation=roster("lead", "author"),
                                         workspace_path=str(self.local)))
            detail = finished(self.svc, run["id"])
        self.assertEqual(detail["run"]["status"], "failed")
        self.assertFalse((self.local / "records.json").exists())
        self.assertEqual(detail["conversation"]["turns"][-1]["artifacts"], [])
        model = RecordingProvider([tool("read_file", path="records.json"), {"text": "recovered original data"}])
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=model):
            later = self.svc.start(request("single", objective="use original records", conversation={
                "parent_run_id": run["id"], "intent": "edit", "recipient": "lead", "recover_failed": True}))
            resumed = finished(self.svc, later["id"])
        self.assertIn('room', str(model.requests[-1][0]))
        self.assertEqual(resumed["conversation"]["turns"][-1]["recovery_source"]["role"], "author")
        self.assertEqual((self.local / "records.json").read_text(), '[{"room":"A"}]')

    def test_publish_conflict_is_visible_and_safe_to_recover(self):
        with patch("miniagent.studio_workspace.publish", side_effect=WorkspaceError("本地文件已发生变化")):
            run = self.svc.start(request(workspace_path=str(self.local)))
            detail = finished(self.svc, run["id"])
        self.assertEqual(detail["run"]["status"], "failed")
        self.assertEqual(detail["run"]["workspace_delivery"]["status"], "failed")
        self.assertEqual(detail["conversation"]["turns"][0]["artifacts"][0]["name"], "notes.md")
        self.assertFalse((self.local / "notes.md").exists())

    def blocked_drafts(self, *, later_publication=False):
        from tests.test_studio_crew import roster, write
        second = "Published peer work." if later_publication else "[BLOCKED] waiting for verification"
        model = RecordingProvider([
            tool("delegate_task", assignee="author", objective="draft A", remaining_roles=["reviewer"]),
            write("a.md", "private work from author"), {"text": "[BLOCKED] waiting for review"},
            tool("delegate_task", assignee="reviewer", objective="draft B", remaining_roles=[]),
            write("b.md", "private work from reviewer"), {"text": second},
            {"text": "[BLOCKED] root still needs review"},
        ])
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=model):
            run = self.svc.start(request("team", objective="collaborate", conversation=roster("lead", "author", "reviewer"),
                                         workspace_path=str(self.local)))
            detail = finished(self.svc, run["id"])
        self.assertEqual(detail["run"]["status"], "failed", detail["diagnostics"])
        ids = {row["role"]: row["task_id"] for row in detail["room_events"] if row["kind"] == "member_report"}
        self.assertEqual(ids.keys(), {"author", "reviewer"})
        return run["id"], ids, detail["conversation"]["recovery_candidates"]

    def recover(self, parent, *, acceptance_files=None, **fields):
        return self.svc.start(request("single", objective="resume", acceptance_files=acceptance_files or [], conversation={
            "parent_run_id": parent, "intent": "edit", "recipient": "lead", "recover_failed": True, **fields}))

    def test_choose_member_task_when_a_failed_group_has_multiple_private_drafts(self):
        parent, ids, candidates = self.blocked_drafts()
        self.assertEqual([(c["task_id"], c["objective"]) for c in candidates],
                         [(ids["reviewer"], "draft B"), (ids["author"], "draft A")])
        with self.assertRaises(StudioError) as error:
            self.recover(parent)
        self.assertEqual(error.exception.code, "recovery_selection_required")
        model = RecordingProvider([tool("read_file", path="a.md"), {"text":"[BLOCKED] another check is needed"}])
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=model):
            run = self.recover(parent, recover_task_id=ids["author"], acceptance_files=["required.md"])
            detail = finished(self.svc, run["id"])
        self.assertIn("private work from author", str(model.requests[-1][0]))
        self.assertFalse((self.svc.runs_dir / run["id"] / "agents/lead/b.md").exists())
        self.assertEqual(detail["conversation"]["turns"][-1]["recovery_source"],
                         {"role": "author", "task_id": ids["author"]})
        self.assertEqual(detail["run"]["status"], "failed")
        self.assertFalse((self.local / "a.md").exists())
        with self.assertRaises(StudioError) as stale:
            self.recover(parent, recover_task_id=ids["author"])
        self.assertEqual(stale.exception.code, "stale_head")

    def test_recovery_rejects_foreign_tasks_paths_symlinks_partial_copies_and_user_edits(self):
        parent, ids, _ = self.blocked_drafts()
        other, foreign, _ = self.blocked_drafts()
        for invalid in [foreign["author"], "../../" + other, parent, False]:
            with self.subTest(invalid=invalid), self.assertRaises(StudioError):
                self.recover(parent, recover_task_id=invalid)
        directories = self.svc.runs_dir / parent / "assignments"
        [leaf] = directories.glob("*/checkpoint/agents/author")
        leaf.rename(leaf.with_name("backup"))
        leaf.symlink_to(leaf.with_name("backup"))
        with self.assertRaises(StudioError) as unsafe:
            self.recover(parent, recover_task_id=ids["author"])
        self.assertEqual(unsafe.exception.code, "invalid_recovery_checkpoint")
        leaf.unlink()
        leaf.with_name("backup").rename(leaf)
        (leaf / "a.md").unlink()
        self.assertEqual([c["task_id"] for c in self.svc.detail(parent)["conversation"]["recovery_candidates"]],
                         [ids["reviewer"]])
        with self.assertRaises(StudioError) as partial:
            self.recover(parent, recover_task_id=ids["author"])
        self.assertEqual(partial.exception.code, "unsupported_snapshot")
        (self.local / "my.md").write_text("human changes")
        with self.assertRaises(StudioError) as changed:
            self.recover(parent, recover_task_id=ids["reviewer"])
        self.assertEqual(changed.exception.code, "workspace_conflict")
        self.assertEqual([p.name for p in self.local.iterdir()], ["my.md"])
        self.assertEqual(len(self.svc.list_runs()["runs"]), 2)

    def test_later_publication_invalidates_older_drafts(self):
        parent, ids, candidates = self.blocked_drafts(later_publication=True)
        self.assertEqual(candidates, [])
        with self.assertRaises(StudioError) as error:
            self.recover(parent, recover_task_id=ids["author"])
        self.assertEqual(error.exception.code, "invalid_recovery_checkpoint")
        self.assertEqual((self.local / "b.md").read_text(), "private work from reviewer")

    def test_same_room_read_only_followup_keeps_selected_author_draft_recoverable(self):
        parent, ids, _ = self.blocked_drafts()
        reader = RecordingProvider([{"text": "This conversation has a private draft."}])
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=reader):
            query = self.svc.start(request("single", objective="Explain the team's workflow.",
                conversation={"parent_run_id": parent, "recipient": "lead", "intent": "question"}))
            finished(self.svc, query["id"])
        self.assertIn(ids["author"], {c["task_id"] for c in self.svc.detail(query["id"])["conversation"]["recovery_candidates"]})
        model = RecordingProvider([tool("read_file", path="a.md"), {"text": "Done."}])
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=model):
            resumed = self.recover(query["id"], recover_task_id=ids["author"])
            detail = finished(self.svc, resumed["id"])
        self.assertEqual(detail["conversation"]["turns"][-1]["base_run_id"], parent)
        self.assertIn("private work from author", str(model.requests[-1][0]))

    def test_legacy_cancelled_status_only_skips_read_receipts_including_human_alias(self):
        import json
        root = self.svc.runs_dir / "legacy-status"
        root.mkdir()
        old = {"intent": "edit", "status": "failed", "run_id": "older"}
        latest = {"intent": "edit", "status": "cancelled", "run_id": root.name}
        (root / "studio.json").write_text(json.dumps({"title": "所以现在完成了吗"}))
        trace = root / "trace.jsonl"
        trace.write_text(json.dumps({"kind": "tool_result", "operation": "ask_human", "tool": "human"}))
        self.assertEqual(self.svc._recoverable_group_turn([old, latest]), old)
        with trace.open("a") as f:
            f.write("\n" + json.dumps({"kind": "tool_start", "tool": "edit_file"}))
        self.assertIsNone(self.svc._recoverable_group_turn([old, latest]))

    def test_recovered_evidence_requires_the_frozen_bytes_and_independent_signer(self):
        from miniagent.studio_reviews import digest
        from miniagent.studio_conversations import SnapshotError
        manifest = {"app.txt": "hash"}
        version = digest(manifest)
        author = {"task_id": "author-task", "role": "dev"}
        signed = {"target": "author-task", "candidate_version": version, "evaluator": "qa", "attempt_id": "attempt-qa",
                  "verdict": "passed", "results": [{"criterion_id": "smoke", "verdict": "passed",
                      "reason": "Browser exercised the frozen file.", "receipts": [{"ref": "Eabc"}]}]}
        author_event = {"kind": "member_report", "task_id": "author-task", "role": "dev",
                        "handoff": {"reviewable_candidate": {"candidate_version": version}}}
        peer = {"kind": "member_report", "role": "qa", "task_id": "qa-task",
                "handoff": {"native_review": {"kind": "product", "state": "blocked", "author": "dev", "reviewer": "qa",
                        "target": "author-task", "candidate_version": version, "assessment": signed}}}
        with patch.object(self.svc, "room_events", return_value=[author_event, peer]):
            receipt = self.svc._checkpoint_assessment(self.private, author, manifest)
            self.assertEqual((receipt["review_task"], receipt["verdict"]), ("qa-task", "passed"))
            self.assertEqual(receipt["findings"][0]["receipts"], ["Eabc"])
            with self.assertRaises(SnapshotError):
                self.svc._checkpoint_assessment(self.private, author, {"app.txt": "changed"})
            signed["verdict"] = "changes_requested"
            signed["results"][0]["verdict"] = "changes_requested"
            self.assertEqual(self.svc._checkpoint_assessment(self.private, author, manifest)["verdict"], "changes_requested")
            signed["evaluator"] = "dev"
            self.assertIsNone(self.svc._checkpoint_assessment(self.private, author, manifest))

    def test_control_directory_credential_and_hidden_paths(self):
        for invalid in (str(self.svc.runs_dir), str(self.svc.runs_dir.parent), "relative"):
            with self.subTest(invalid=invalid), self.assertRaises(StudioError):
                self.svc.start(request(workspace_path=invalid))
        (self.local / "leak.txt").write_text("fake-service-key")
        with self.assertRaises(StudioError):
            self.svc.start(request(workspace_path=str(self.local)))
