"""A passed product and a revised reviewer test have separate obligations."""
import hashlib
import json
import re
import unittest
from unittest.mock import Mock, patch

from miniagent.config import Settings
from miniagent.studio_crew import RoomCrew
from miniagent.studio_reviews import Review, ReviewQueue, Rule, digest
from miniagent.studio_service import StudioService
from miniagent.rao import Budget, Observation
from tests.support import cleanup, make_temp_dir
from tests.test_studio_conversations import RecordingProvider, tool
from tests.test_studio_crew import roster, write
from tests.test_studio_service import finished, request


class PeerChecksTest(unittest.TestCase):
    def room(self):
        room = object.__new__(RoomCrew)
        room.direct, room.read_only, room.thread, room.direct_candidate = False, False, {}, None
        room.lead = "lead"
        room.profiles = {r: {} for r in ("lead", "author", "qa", "peer")}
        room.plan_order = ["author", "qa", "peer"]
        room.planned, room.assigned, room.approved_roles = ({"author", "qa"} for _ in range(3))
        room.covered_roles, room.child_checks = set(), {}
        room.delivery_issue = ""
        room.delivery = {"files": ["label.txt"]}
        room.run_id, room.room_events = "run", lambda _: []
        room.handoffs, room.private_drafts = {}, {}
        room.project_version, room.inherited_review = 1, None
        room.settings = Settings(api_key="test-only")
        room.extensions = {".txt", ".md", ".py"}
        room.assignment_results, room.untapped_shares = {}, {}
        room.started_roles, room.nested_seconds = set(), 0
        room.should_cancel = lambda: False
        room._dispatch_limit_reached = lambda: False
        room._allocation = lambda *_: (Budget.from_limits(room.settings.limits), Budget.from_limits(room.settings.limits))
        room._dispatch = Mock(return_value=Observation(step_id="step", source="delegate", ok=True, content="delegated"))
        room.record_event = Mock()
        room.reports = [{"published": True, "role": "author"}]
        room.native_reviews = ReviewQueue()
        review = Review("product-task", "author", "qa", "Deliver the label", [
            Rule(id="label", description="Confirm shipment", method="inspect")], {"label.txt": "sha"})
        review.state, review.latest = "passed", {"candidate_version": review.version,
            "results": [{"criterion_id": "label", "verdict": "passed", "receipts": []}]}
        room.native_reviews.items[review.target] = review
        room.native_reviews.public_versions = dict(review.files)
        room.pending_check_reviews = {"qa-task": {"task": "qa-task", "author": "qa", "changes": [],
            "candidate": {"check.py": "current"}, "verification": [{"scope": "exact-scope",
                "selection": "selection", "check": "python3 check.py", "path": "check.py",
                "at_sha256": "original", "current_sha256": "current"}]}}
        return room

    def test_briefing_identifies_separate_obligation_and_eligible_peers(self):
        room = self.room()
        text = room.before_answer()
        for needle in ("附属检查", "省略 review", "exact-scope", "python3 check.py", "current", "[CHECKED qa-task]"):
            self.assertIn(needle, text)
        row, = room._pending_check_briefing()
        self.assertEqual(row["eligible_roles"], ["author", "peer"])
        receipt, = room._passed_review_receipts()
        self.assertEqual((receipt["target"], receipt["state"]), ("product-task", "passed"))
        room.native_reviews.public_versions["label.txt"] = "new-version"
        self.assertEqual(room._passed_review_receipts(), [])

    def test_pending_product_or_member_or_source_gate_is_not_bypassed(self):
        room = self.room()
        self.assertTrue(room._only_peer_checks_remaining())
        review = room.native_reviews.items["product-task"]
        review.state = "review"
        self.assertFalse(room._only_peer_checks_remaining())
        self.assertIn("独立验收尚未通过", room.before_answer())
        review.state = "passed"
        room.planned = {"author", "qa", "peer"}
        self.assertFalse(room._only_peer_checks_remaining())
        room.approved_roles = set(room.planned)
        room.delivery_issue = "actual delivery failed"
        self.assertFalse(room._only_peer_checks_remaining())
        room.delivery_issue = ""
        room.native_reviews.required_source_paths = {"label.txt"}
        self.assertFalse(room._only_peer_checks_remaining())

    def test_missing_or_partial_product_approval_cannot_trigger_routing(self):
        room = self.room()
        room.delivery["files"].append("another-product.txt")
        self.assertFalse(room._only_peer_checks_remaining())
        room.delivery = {}
        self.assertFalse(room._only_peer_checks_remaining())
        room.delivery = {"files": ["label.txt"]}
        room.native_reviews.items.clear()
        self.assertFalse(room._only_peer_checks_remaining())

    def test_ordinary_assignment_preserves_objective_files_and_recipient(self):
        room = self.room()
        result = room.delegate({"assignee": "peer", "objective": "Write the requested deployment guide",
                                "deliverable_paths": ["deploy.md"], "acceptance": "Explain deployment",
                                "__runtime_action_id": "action"})
        self.assertTrue(result.ok)
        call = room._dispatch.call_args.args
        self.assertEqual(call[:3], ("peer", "Write the requested deployment guide", "Explain deployment"))
        self.assertEqual(call[6], ["deploy.md"])
        room.record_event.assert_not_called()

    def test_routes_only_obligations_the_recipient_can_independently_check(self):
        room = self.room()
        room.pending_check_reviews["author-task"] = {
            "task": "author-task", "author": "author", "last_editor": "qa", "changes": [],
            "verification": [{"scope": "other-scope", "path": "other.py", "check": "python3 other.py", "current_sha256": "other"}],
            "candidate": {f"file-{i}.txt": "irrelevant" for i in range(100)}}
        result = room.delegate({"assignee": "author", "objective": "Reopen the product",
                                "review": {"candidate": "product-task", "criteria": [
                                    c.model_dump() for c in room.native_reviews.items["product-task"].criteria]},
                                "__runtime_action_id": "action"})
        self.assertTrue(result.ok)
        role, objective = room._dispatch.call_args.args[:2]
        self.assertEqual(role, "author")
        self.assertIn("[CHECKED qa-task]", objective)
        self.assertNotIn("author-task", objective)
        self.assertNotIn("delegate_task", objective)
        self.assertNotIn("file-99", room._peer_check_instruction())
        self.assertEqual(set(room.pending_check_reviews), {"qa-task", "author-task"})

    def test_routed_peer_does_not_resume_its_own_unrelated_private_draft(self):
        room = self.room()
        room.private_drafts["author"] = {"review_after_repair": True, "version": room.project_version,
                                          "task_id": "unrelated-draft"}
        result = room.delegate({"assignee": "author", "objective": "Reopen the product",
                                "review": {"candidate": "product-task", "criteria": [
                                    c.model_dump() for c in room.native_reviews.items["product-task"].criteria]},
                                "__runtime_action_id": "action"})
        self.assertTrue(result.ok)
        self.assertIsNone(room._dispatch.call_args.args[7])  # no private draft
        self.assertIsNone(room._dispatch.call_args.args[9])  # no new native product review

    def test_passed_receipt_omits_incidental_manifest_files(self):
        room = self.room()
        item = room.native_reviews.items["product-task"]
        item.files.update({f"unrelated-{i}.txt": "digest" for i in range(100)})
        item.latest["candidate_version"] = item.version
        room.native_reviews.public_versions = dict(item.files)
        receipt, = room._passed_review_receipts()
        self.assertEqual(receipt["files"], {"label.txt": "sha"})
        self.assertEqual(receipt["candidate_file_count"], 101)

    def test_new_criteria_on_the_same_product_are_not_silently_discarded(self):
        room = self.room()
        selection = {"candidate": "product-task", "criteria": [
            {"id": "tax", "method": "source", "description": "Check a newly required tax source"}]}
        with patch.object(room.native_reviews, "open", side_effect=ValueError("already bound")) as opened:
            result = room.delegate({"assignee": "qa", "objective": "New business requirement",
                                    "review": selection, "__runtime_action_id": "action"})
        self.assertFalse(result.ok)
        self.assertEqual(opened.call_args.args[0], selection)
        room.record_event.assert_not_called()

    def test_author_alias_does_not_implicitly_identify_a_passed_product(self):
        room = self.room()
        selection = {"candidate": "author", "criteria": []}
        with patch.object(room.native_reviews, "open", side_effect=ValueError("ambiguous author")) as opened:
            result = room.delegate({"assignee": "qa", "objective": "A different product by the same author",
                                    "review": selection, "__runtime_action_id": "action"})
        self.assertFalse(result.ok)
        self.assertEqual(opened.call_args.args[0], selection)
        self.assertIn("eligible_roles", result.content)
        room.record_event.assert_not_called()

    def test_different_product_review_keeps_its_criteria_and_error(self):
        room = self.room()
        selection = {"candidate": "different-product", "criteria": [{"id": "different", "method": "execute",
                                                                    "description": "Run another product"}]}
        with patch.object(room.native_reviews, "open", side_effect=ValueError("different product needs its own receipt")) as opened:
            result = room.delegate({"assignee": "peer", "objective": "Check a different product",
                                    "review": selection, "__runtime_action_id": "action"})
        self.assertFalse(result.ok)
        self.assertIn("different product", result.content)
        self.assertEqual(opened.call_args.args[:2], (selection, "peer"))
        room.record_event.assert_not_called()

    def test_no_independent_recipient_cannot_clear_the_obligation(self):
        room = self.room()
        room.profiles = {r: {} for r in ("lead", "author", "qa")}
        room.pending_check_reviews["qa-task"]["last_editor"] = "author"
        result = room.delegate({"assignee": "qa", "objective": "Reopen same review", "__runtime_action_id": "action",
                                "review": {"candidate": "product-task", "criteria": [
                                    c.model_dump() for c in room.native_reviews.items["product-task"].criteria]}})
        self.assertFalse(result.ok)
        self.assertIn("没有可独立核销", result.content)
        self.assertIn("qa-task", room.pending_check_reviews)
        room.record_event.assert_not_called()

    def test_normal_entry_routes_reopened_passed_product_to_a_real_independent_run(self):
        root = make_temp_dir("peer-check-route")
        self.addCleanup(cleanup, root)
        service = StudioService(root / "runs", Settings(api_key="test-only"))
        self.addCleanup(service.close)
        label = "Shipment destination: confirmed warehouse."
        rule = {"id": "label", "method": "inspect", "description": "Confirm the warehouse label."}
        old = "assert 'wrong' in open('label.txt').read()\nprint('1 passed')\n"
        new = old.replace("'wrong'", "'confirmed'")
        script = [
            tool("delegate_task", assignee="author", objective="Write label", remaining_roles=["qa"], deliverable_paths=["label.txt"]),
            write("label.txt", label), {"text": "Label ready."},
            tool("delegate_task", assignee="qa", objective="Independently inspect label", remaining_roles=[],
                 review={"candidate": "author", "criteria": [rule]}),
            tool("read_file", path="label.txt"), write("check.py", old),
            tool("run_shell", command='python3 check.py > check.log 2>&1; echo "EXIT=$?"; tail -3 check.log'),
            tool("edit_file", path="check.py", old_text="'wrong'", new_text="'confirmed'"),
            tool("run_shell", command="python3 check.py"),
            tool("submit_review", candidate_version=digest({"label.txt": hashlib.sha256(label.encode()).hexdigest()}),
                 results=[{"criterion_id": "label", "verdict": "passed", "reason": "Read the confirmed warehouse label.",
                           "evidence": ["file:label.txt"]}]),
            {"text": "Product passed; corrected my checker and retained its verification obligation."},
            tool("delegate_task", assignee="qa", objective="Reopen the same full product review",
                 deliverable_paths=["another-full-report.md"], review={"candidate": "label.txt", "criteria": [rule]}),
            tool("read_file", path="check.py"), tool("read_file", path="label.txt"),
            tool("run_shell", command="python3 check.py"), {"text": "__CHECKED__"}, {"text": "Delivered reviewed label."},
        ]

        class PeerProvider(RecordingProvider):
            def chat(self, messages, tools=None, temperature=0):
                if self.cursor < len(self.script) and self.script[self.cursor].get("text") == "__CHECKED__":
                    matches = re.findall(r"\[CHECKED (task-[a-z0-9]+)\]", str(messages))
                    assert matches, "host must identify the exact remaining obligation"
                    self.script[self.cursor] = {"text": "[CHECKED " + matches[-1] + "] Read original requirement and unchanged checker; executed it successfully."}
                return super().chat(messages, tools, temperature)

        provider = PeerProvider(script)
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider):
            run = service.start(request("team", conversation=roster("lead", "author", "qa"),
                                        objective="Deliver independently checked label", acceptance_files=["label.txt"],
                                        limits={"max_steps": 90}))
            detail = finished(service, run["id"])
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        routed, = [e for e in detail["room_events"] if e["kind"] == "peer_check_routed"]
        self.assertEqual((routed["requested"], routed["recipient"]), ("qa", "author"))
        reports = [e["handoff"] for e in detail["room_events"] if e["kind"] == "member_report"]
        self.assertEqual([r["role"] for r in reports], ["author", "qa", "author"])
        self.assertTrue(reports[1]["evidence"]["pending_verification"])
        self.assertEqual(reports[1]["native_review"]["state"], "passed")
        self.assertEqual(reports[2]["independently_reviewed_tasks"], [reports[1]["task_id"]])
        self.assertEqual(reports[2]["deliverable_paths"], [])
        self.assertEqual(reports[2]["changed_files"], [])
        public = root / "runs" / run["id"] / "agents" / "lead"
        self.assertEqual((public / "label.txt").read_text(), label)
        self.assertEqual((public / "check.py").read_text(), new)
        self.assertFalse((public / "another-full-report.md").exists())
        self.assertTrue(any("已通过且仍匹配当前文件" in str(messages) and "eligible_roles" in str(messages)
                            for messages, _ in provider.requests))


if __name__ == "__main__":
    unittest.main()
