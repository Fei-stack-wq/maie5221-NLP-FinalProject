"""Read real on-disk journals without launching a task or opening the shared project."""
import json
import tempfile
import threading
import unittest
from pathlib import Path

from miniagent.studio_discussion import RoomDiscussion
from miniagent.studio_room_context import published_artifact_catalog, room_context
from miniagent.studio_service import StudioService


class Journal(RoomDiscussion):
    _directory = StudioService._directory
    _projection_basis = StudioService._projection_basis

    def __init__(self, root):
        self.runs_dir = root
        self._lock = threading.RLock()

    def _meta(self, path):
        return json.loads((path / "studio.json").read_text())

    def _redact(self, value):
        return value


class RoomContextTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Journal(Path(tmp.name))
        self.room = {"kind": "group", "id": "original", "lead": "lead", "members": ["lead", "qa"], "turns": [],
                     "messages": [], "profiles": [{"id": "qa", "name": "Quality", "prompt": "Independent review"}]}
        self.save("original", "original", "this room", "ready", mode="room")

    def save(self, identity, group, title, state, *, mode="team", declared=(), published=(), events=(), reply=None):
        path = self.store.runs_dir / identity
        path.mkdir()
        thread = {"id": group, "kind": "group", "ordinal": len(self.room["turns"]) + 1, "intent": "edit", "recipient": "lead"}
        meta = {"mode": mode, "thread": thread, "title": title, "state": state,
                "request": {"acceptance_files": list(declared)}}
        if published:
            meta["workspace_delivery"] = {"status": "published", "files": list(published)}
        (path / "studio.json").write_text(json.dumps(meta))
        (path / "public-room.jsonl").write_text("".join(json.dumps(row) + "\n" for row in events))
        if reply is not None:
            (path / "conversation-answer.txt").write_text(reply)
        if mode != "room":
            self.room["turns"].append({"run_id": identity, "status": state})
        return path

    def test_cancelled_edits_do_not_mask_completed_delivery_or_inject_private_files(self):
        files = ["meeting/index.html", "meeting/QA_REPORT.md"]
        self.save("accepted", "original", "Ship the meeting tool", "completed", declared=files, published=files,
                  reply="Frozen app passed; cancellation frees a slot.",
                  events=[{"kind": "member_report", "role": "qa", "task_id": "q1", "published": True,
                           "handoff": {"role": "qa", "task_id": "q1", "changed_files": [
                               {"path": files[1], "change": "added"}], "summary": "54 browser checks passed"}}])
        version = "a" * 64
        rows = [{"kind": "assignment", "role": "lead", "task_id": "draft", "recipient": "qa"},
                {"kind": "member_report", "role": "qa", "task_id": "draft", "published": False,
                 "handoff": {"role": "qa", "task_id": "draft", "deliverable_paths": ["private.md"],
                             "reviewable_candidate": {"task_id": "draft", "candidate_version": version}}}]
        self.save("cancelled", "original", "你都做了啥", "cancelled", events=rows, reply="Fake published claim")
        # Even a malformed room projection must not admit foreign records to a model request.
        self.save("other", "foreign", "another group's private query", "completed", published=["unrelated.doc"])
        (self.store.runs_dir / "accepted" / "never-open-this.txt").write_text("OTHER WORKSPACE CONTENT")
        result = room_context(self.store, self.room, text="你都做了啥", recipient="qa")
        self.assertEqual((result["latest_published_task_id"], result["task_count"]), ("accepted", 2))
        self.assertEqual(result["responding_role"], "qa")
        accepted, cancelled = result["tasks"]
        self.assertEqual(accepted["published_files"], files)
        self.assertEqual(accepted["member_handoffs"][0]["changed_files"][0]["path"], files[1])
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(cancelled["published_files"], [])
        self.assertEqual(cancelled["frozen_candidates"][0]["version"], version)
        serialized = json.dumps(result)
        for secret in ("another group's", "unrelated.doc", "Fake published claim", "OTHER WORKSPACE CONTENT"):
            self.assertNotIn(secret, serialized)

    def test_older_topics_and_prior_chat_survive_multiple_tasks_and_reload(self):
        self.save("initial", "original", "original design", "completed")
        self.save("early", "original", "发布财务看板", "completed", declared=["指标表.md"], reply="Old conclusion")
        self.room["messages"] = [{"kind": "group_message", "question": "Why quarterly?",
                                  "role": "qa", "text": "Board policy.", "request_id": "first",
                                  "at": "2020-01-01", "disposition": "reply"}]
        for n in range(12):
            self.save("recent-" + str(n), "original", "Review the version", "completed")
            self.room["messages"].append({"kind": "group_message", "role": "lead", "question": "Fine",
                                          "text": "Saved", "request_id": str(n), "at": f"2020-02-{n+1:02}"})
        self.save("busy", "original", "Implement later item", "running")
        result = room_context(Journal(self.store.runs_dir), self.room, text="当时财务看板的结果呢？")
        self.assertEqual(result["active_task_id"], "busy")
        self.assertEqual(result["task_count"], 15)
        older = next(row for row in result["tasks"] if row["run_id"] == "early")
        self.assertEqual(older["published_files"], ["指标表.md"])
        self.assertEqual(older["answer"], "Old conclusion")
        self.assertTrue(any(row["text"] == "Board policy." for row in result["recent_messages"]))
        self.assertLess(len(result["tasks"]), len(self.room["turns"]))

    def test_failed_root_is_distinct_from_passed_private_review_and_historical_commentary(self):
        required = ["meeting/index.html", "meeting/QA_REPORT.md", "meeting/RELEASE_NOTES.md"]
        version = "c" * 64
        signed = {"target": "build", "candidate_version": version, "evaluator": "qa",
                  "attempt_id": "independent-qa", "verdict": "passed", "results": [{}, {}, {}]}
        self.save("root-failed", "original", "implement booking", "failed", declared=required,
                  reply="Assertions in the app failed.",
                  events=[{"kind": "assignment", "task_id": "build", "role": "lead", "recipient": "dev"},
                          {"kind": "member_report", "role": "dev", "task_id": "build", "published": False,
                           "handoff": {"task_id": "build", "role": "dev", "deliverable_paths": required[:1],
                                       "reviewable_candidate": {"task_id": "build", "candidate_version": version}}},
                          {"kind": "assignment", "task_id": "review", "role": "lead", "recipient": "qa"},
                          {"kind": "member_report", "role": "qa", "task_id": "review", "published": False,
                           "handoff": {"task_id": "review", "role": "qa", "native_review": {
                               "reviewer": "qa", "author": "dev", "candidate_version": version,
                               "target": "build", "assessment": signed, "files": {required[0]: "d" * 64}}}},
                          {"kind": "assistant_progress", "role": "lead", "text": "Assertions in the app failed."}])
        self.save("shipped", "original", "finish two documents", "completed", declared=required, published=required)
        failed = room_context(self.store, self.room, text="what was delivered?")["tasks"][0]
        root = failed["root_outcome"]
        self.assertEqual((root["state"], root["acceptance_verdict"]), ("failed", None))
        self.assertEqual(root["required_only_in_frozen_draft"], required[:1])
        self.assertEqual(root["required_missing_from_public_and_frozen_draft"], required[1:])
        self.assertEqual(root["required_present_in_public_project"], [])
        self.assertEqual((failed["reviews"][0]["verdict"], failed["reviews"][0]["criteria"]), ("passed", 3))
        self.assertNotIn("Assertions in the app failed", str(failed))
        self.assertEqual(failed["published_artifacts"], [])

    def test_version_catalog_binds_latest_receipt_to_change_author_and_archive(self):
        name, digest, snapshot = "report.md", "b" * 64, "c" * 32

        def receipt(task, sha, *, role="qa", artifact_name=name, change="added"):
            return {"kind": "member_report", "role": role, "task_id": task, "published": True,
                    "handoff": {"task_id": task, "role": role, "changed_files": [
                        {"path": name, "sha256": sha, "change": change}]},
                    "artifacts": [{"project_path": artifact_name, "sha256": digest,
                                   "path": f"handoffs/{snapshot}/{artifact_name}"}]}

        first = receipt("q1", digest)
        self.save("good", "original", "ship", "completed", events=[first])
        self.save("newer", "original", "update", "completed", events=[first, receipt("q2", "d" * 64)])
        self.save("removed", "original", "remove", "completed", events=[first, receipt("q3", None, change="deleted")])
        for run_id, want in (("good", [name]), ("newer", []), ("removed", [])):
            task = next(t for t in room_context(self.store, self.room, text=run_id)["tasks"] if t["run_id"] == run_id)
            self.assertEqual([a["path"] for a in task["published_artifacts"]], want)
            if want:
                self.assertEqual(task["published_artifacts"][0]["sha256"], digest)

    def test_group_detail_can_link_inherited_frozen_file_first_published_in_this_turn(self):
        name, inherited, deleted, stale = "DEV_SELFTEST.md", "older.txt", "gone.txt", "report.md"
        previous, latest = "a" * 64, "d" * 64
        self.save("first", "original", "build", "failed")
        final = self.save("delivered", "original", "ship documents with the frozen product", "completed",
                          published=[name, inherited, deleted, stale])
        baseline = {"directory": [1, 2], "files": {inherited: previous, deleted: previous}}
        projection = {"directory": [1, 2], "files": {name: latest, inherited: previous, stale: latest}}
        (final / "workspace-baseline.json").write_text(json.dumps(baseline))
        (final / "workspace-projection.json").write_text(json.dumps(projection))
        other_revision = {"kind": "member_report", "role": "qa", "task_id": "review", "published": True,
                          "handoff": {"role": "qa", "task_id": "review",
                                      "changed_files": [{"path": stale, "change": "added", "sha256": previous}]},
                          "artifacts": [{"path": "handoffs/" + "c" * 32 + "/" + stale,
                                         "project_path": stale, "sha256": previous}]}
        meta = self.store._meta(final)
        catalog = published_artifact_catalog(self.store, final, meta["thread"], meta, "completed", [other_revision])
        self.assertEqual(catalog, [{"run_id": final.name, "path": n, "ref": "agents/lead/" + n}
                                   for n in (name, stale)])

    def test_requested_artifact_survives_large_version_catalog(self):
        names = [f"item-{i:02d}.md" for i in range(42)]
        task = self.save("large", "original", "many reports", "completed", published=names)
        (task / "workspace-baseline.json").write_text(json.dumps({"directory": [1, 2], "files": {}}))
        (task / "workspace-projection.json").write_text(json.dumps({
            "directory": [1, 2], "files": dict.fromkeys(names, "b" * 64)}))
        result = room_context(self.store, self.room, text="打开 item-41.md")
        candidates = result["tasks"][0]["published_artifacts"]
        self.assertEqual(candidates[0]["path"], "item-41.md")
        self.assertLess(len(candidates), len(names))
