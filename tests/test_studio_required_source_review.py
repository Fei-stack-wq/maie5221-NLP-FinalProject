"""Acceptance of the final manuscript is independent of earlier research notes."""
import hashlib
import unittest
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.research import Page
from miniagent.studio_reviews import ReviewQueue, digest
from miniagent.studio_service import StudioError, StudioService
from tests.support import cleanup, make_temp_dir
from tests.test_studio_conversations import RecordingProvider, tool
from tests.test_studio_crew import roster, write
from tests.test_studio_service import finished, request


FINAL, NOTES = "final.md", "notes.md"
ARTICLE = "This shipment goes to Main Street. Independently sourced final article."
NOTE = "Working notes for the shipment, including the approved destination."
URL = "https://example.org/dispatch"
RULE = {"id": "claim", "description": "Verify the destination against the final dispatch order.", "method": "source"}


def hashed(text):
    return hashlib.sha256(text.encode()).hexdigest()


def submit(manifest, path):
    return tool("submit_review", candidate_version=digest(manifest), results=[
        {"criterion_id": "claim", "verdict": "passed", "evidence": ["file:" + path, "source:" + URL],
         "reason": "Compared the cited destination against the signed dispatch order."}])


class RequiredSourceReviewTest(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir("required-sources")
        self.service = StudioService(self.root / "runs", Settings(api_key="test-credential"))
        self.addCleanup(cleanup, self.root)
        self.addCleanup(self.service.close)

    def execute(self, steps, *, members=("lead", "writer", "checker"), **kwargs):
        provider = RecordingProvider(steps)
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider), \
             patch("miniagent.research.fetch", return_value=Page(URL, "text/plain", ARTICLE.encode(), False)):
            job = self.service.start(request("team", conversation=roster(*members),
                objective="Publish the sourced dispatch article.", required_source_review_paths=[FINAL],
                limits={"max_steps": 90}, **kwargs))
            return finished(self.service, job["id"]), provider

    def test_final_file_requires_fresh_source_and_read_of_actual_manuscript(self):
        files = {FINAL: hashed(ARTICLE), NOTES: hashed(NOTE)}
        review = {"candidate": "writer", "criteria": [RULE]}
        steps = [tool("delegate_task", assignee="writer", objective="Write final article and notes.",
                      deliverable_paths=[FINAL, NOTES], remaining_roles=[]),
                 write(FINAL, ARTICLE), write(NOTES, NOTE), {"text": "Article and notes ready."},
                 {"text": "The notes were checked previously; ready."},
                 tool("delegate_task", assignee="checker", objective="Check notes.",
                      review={**review, "criteria": [{**RULE, "method": "inspect"}]}),
                 tool("delegate_task", assignee="writer", objective="Approve my own article.", review=review),
                 tool("delegate_task", assignee="checker", objective="Verify current article.", review=review),
                 tool("read_file", path=NOTES), tool("http_get", url=URL), submit(files, NOTES),
                 tool("read_file", path=FINAL), submit(files, FINAL), {"text": "Published assessment."},
                 {"text": "The final article is ready."}]
        detail, provider = self.execute(steps)
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        messages = str(provider.requests)
        for expected in ("指定成稿当前版本缺少独立来源核查", "请加入必需的 source 标准", "请选择本群其他成员",
                         "引用实际读过的成稿和本次打开的原始来源"):
            self.assertIn(expected, messages)
        report = [e["handoff"] for e in detail["room_events"] if e["kind"] == "member_report"][-1]
        review = report["native_review"]
        self.assertEqual(review["state"], "passed")
        receipts = review["assessment"]["results"][0]["receipts"]
        self.assertEqual([(r["path"], r["sha256"]) for r in receipts if r["kind"] == "read"], [(FINAL, files[FINAL])])
        self.assertEqual([r["url"] for r in receipts if r["kind"] == "source"], [URL])

    def test_working_notes_alone_cannot_complete_final_delivery(self):
        steps = [tool("delegate_task", assignee="writer", objective="Write research notes.", deliverable_paths=[NOTES],
                      remaining_roles=["checker"]), write(NOTES, NOTE), {"text": "Notes ready."},
                 tool("delegate_task", assignee="checker", objective="Check notes.",
                      review={"candidate": "writer", "criteria": [RULE]}),
                 tool("read_file", path=NOTES), tool("http_get", url=URL), submit({NOTES: hashed(NOTE)}, NOTES),
                 {"text": "Verified notes."}, {"text": "[BLOCKED] No final article was delivered."}]
        detail, _ = self.execute(steps)
        reviews = [e["handoff"] for e in detail["room_events"] if e["kind"] == "member_report"]
        self.assertEqual(reviews[-1]["native_review"]["state"], "passed")
        self.assertEqual(detail["run"]["status"], "failed")

    def test_later_editor_must_get_a_new_review_on_changed_delivery(self):
        new = ARTICLE + " The dispatch order was confirmed."
        review = {"candidate": "writer", "criteria": [RULE]}
        steps = [tool("delegate_task", assignee="writer", objective="Write dispatch article.",
                      deliverable_paths=[FINAL], remaining_roles=["checker", "reviser"]),
                 write(FINAL, ARTICLE), {"text": "First draft ready."},
                 tool("delegate_task", assignee="checker", objective="Verify draft.", review=review),
                 tool("read_file", path=FINAL), tool("http_get", url=URL), submit({FINAL: hashed(ARTICLE)}, FINAL),
                 {"text": "First draft verified."},
                 tool("delegate_task", assignee="reviser", objective="Add confirmation.", deliverable_paths=[FINAL]),
                 write(FINAL, new), {"text": "New version ready."},
                 tool("delegate_task", assignee="checker", objective="Verify changed text."),
                 tool("read_file", path=FINAL), tool("http_get", url=URL),
                 submit({FINAL: hashed(ARTICLE)}, FINAL), submit({FINAL: hashed(new)}, FINAL),
                 {"text": "The new version agrees with the dispatch order."}, {"text": "Delivery complete."}]
        detail, provider = self.execute(steps, members=("lead", "writer", "checker", "reviser"))
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        signed = [e["handoff"]["native_review"]["assessment"] for e in detail["room_events"]
                  if e["kind"] == "member_report" and e["role"] == "checker"]
        self.assertEqual([a["candidate_version"] for a in signed],
                         [digest({FINAL: hashed(ARTICLE)}), digest({FINAL: hashed(new)})])
        self.assertIn("候选版本已改变", str(provider.requests))

    def test_new_version_invalidates_receipt_and_no_op_handoff_cannot_hide_authorship(self):
        old, new = hashed(ARTICLE), hashed(ARTICLE + " Updated.")
        queue = ReviewQueue([FINAL])
        a = {"task_id": "first", "role": "writer", "objective": "Write.", "deliverable_paths": [FINAL]}
        queue.published(a, {FINAL: old}, [{"path": FINAL, "sha256": old}])
        r = queue.open({"candidate": "first", "criteria": [RULE]}, "checker", [a])
        queue.items[r.target] = r
        r.apply({"candidate_version": r.version, "results": [{"criterion_id":"claim", "verdict": "passed",
                 "receipts":[{"kind":"read", "path": FINAL, "sha256": old, "extent": 8}, {"kind":"source"}]}]}, "[]")
        self.assertEqual(queue.missing_source_reviews(), [])
        edit = {**a, "task_id":"updated"}
        queue.published(edit, {FINAL: new}, [{"path": FINAL, "sha256": new}])
        self.assertEqual(queue.missing_source_reviews(), [FINAL])
        proxy = {**edit, "task_id":"proxy", "role":"checker"}
        queue.published(proxy, {FINAL: new}, [])
        with self.assertRaisesRegex(ValueError, "这名成员写入"):
            queue.open({"candidate":"proxy","criteria":[RULE]}, "writer", [edit, proxy])

    def test_invalid_paths_and_unavailable_peer_rejected_before_start(self):
        for value in ("x.md", [None], [FINAL, FINAL], ["../unsafe.md"], ["node_modules/page.md"], ["picture.png"]):
            with self.subTest(value=value), self.assertRaises(StudioError):
                self.service.start(request("team", objective="Publish", conversation=roster("lead", "writer", "checker"),
                                           required_source_review_paths=value))
        for mode, group in [("team", roster("lead", "writer")), ("single", roster("lead", "writer", "checker"))]:
            with self.subTest(mode=mode), self.assertRaises(StudioError):
                self.service.start(request(mode, objective="Publish", conversation=group, required_source_review_paths=[FINAL]))
        legacy = request("team", objective="Publish", conversation=roster("lead", "writer"))
        self.assertEqual(self.service._request(legacy), self.service._request({**legacy,"required_source_review_paths":[]}))
