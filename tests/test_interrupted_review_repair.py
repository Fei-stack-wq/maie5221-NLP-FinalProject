"""A provider deadline on a bound author is recoverable from verified room state."""
import time
import unittest
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.provider_http import ProviderError
from miniagent.studio_crew import RoomCrew
from miniagent.studio_service import StudioService
from tests.support import cleanup, make_temp_dir
from tests.test_studio_conversations import RecordingProvider, tool
from tests.test_studio_crew import roster, write
from tests.test_studio_native_review import PATH, RULE, submit
from tests.test_private_candidate_review import initial, NEW, OLD, RECORDS, submit as submit_private
from tests.test_studio_service import finished, request


class TypedFailureProvider(RecordingProvider):
    def chat(self, messages, tools=None, temperature=0):
        event = self.script[self.cursor] if self.cursor < len(self.script) else {}
        if "provider_failure" in event:
            self.cursor += 1
            # Identical text for all failures. Only the adapter's typed code may route a retry.
            raise ProviderError("Provider request failed", code=event["provider_failure"], attempts=1)
        return super().chat(messages, tools, temperature)


class InterruptedReviewRepairTest(unittest.TestCase):
    old = "Destination: Old Warehouse. Check before dispatch."
    new = "Destination: New Warehouse. Check before dispatch."
    deadline = {"provider_failure": "deadline_exceeded"}

    def execute(self, repair_steps, *, remaining=334.56, accepted=False, cancel=False):
        root = make_temp_dir("provider-interrupted-repair")
        self.addCleanup(cleanup, root)
        project = root / "project"
        project.mkdir()
        service = StudioService(root/"runs", Settings(api_key="test-credential"))
        self.addCleanup(service.close)
        begin = [tool("delegate_task", assignee="author", objective="Write label.", deliverable_paths=[PATH]),
                 write(PATH, self.old), {"text":"Original label ready."},
                 tool("delegate_task", assignee="risk", objective="Check label.", review={"candidate":PATH,"criteria":[RULE]}),
                 tool("read_file", path=PATH), submit(self.old, "changes_requested", cause="product", action="Use New Warehouse."),
                 {"text":"Old destination must be repaired."}]
        delegated = []
        for attempt in repair_steps:
            delegated.extend([tool("delegate_task", assignee="author", objective="Apply the signed destination change."), *attempt])
        if accepted:
            delegated += [tool("delegate_task", assignee="risk", objective="Independently verify the change."),
                          tool("read_file", path=PATH), submit(self.new, "passed"), {"text":"Signed delivery ready."}]
        else:
            delegated += [{"text":"[BLOCKED] The outstanding change lacks an independent signature."}]
        provider = TypedFailureProvider(begin + delegated)
        resume = RoomCrew._resume_interrupted_repair

        def interrupted(crew, review, frozen, progress):
            crew.deadline = time.monotonic() + remaining
            if cancel:
                service.cancel(crew.run_id)
            return resume(crew, review, frozen, progress)

        with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider), \
             patch.object(RoomCrew, "_resume_interrupted_repair", interrupted):
            run = service.start(request("team", conversation=roster("lead", "author", "risk"),
                                        objective="Release the independently verified shipping label.",
                                        workspace_path=str(project), acceptance_files=[PATH],
                                        limits={"max_seconds":3600}))
            detail = finished(service, run["id"])
        events = [e for e in detail["room_events"] if e["kind"] == "member_report"]
        reports = [e["handoff"] for e in events]
        return detail, reports, (project/PATH).read_text()

    def test_same_frozen_html_with_new_executed_evidence_can_resume_and_sign(self):
        check = tool("run_python", code="from pathlib import Path\nassert 'New Warehouse' in Path('delivery.txt').read_text()\nprint('PASS destination')")
        detail, reports, public = self.execute([
            [write(PATH, self.new), self.deadline], [check, self.deadline],
            [tool("read_file", path=PATH), {"text":"The frozen revision is ready for independent review."}]], accepted=True)
        self.assertEqual(detail["run"]["status"], "completed", [(r["role"],r["remaining_issue"]) for r in reports])
        self.assertEqual([r["role"] for r in reports], ["author","risk","author","author","author","risk"])
        first, second = reports[2:4]
        self.assertEqual(first["reviewable_candidate"]["candidate_version"], second["reviewable_candidate"]["candidate_version"])
        self.assertTrue(all(r["interrupted_repair"]["resume"] and not r["published"] for r in (first,second)))
        receipt = second["interrupted_repair"]
        self.assertAlmostEqual(receipt["reserved_seconds"], 105)
        self.assertAlmostEqual(receipt["remaining_seconds"], 334.56, delta=.1)
        self.assertAlmostEqual(receipt["member_seconds"]["author"], 114.78, delta=.1)
        self.assertGreater(receipt["member_seconds"]["risk"], 0)
        self.assertEqual(reports[-1]["native_review"]["assessment"]["verdict"], "passed")
        self.assertEqual(public, self.new)

    def test_identical_checkpoint_with_only_reads_and_a_new_tool_id_stops(self):
        detail, reports, public = self.execute([
            [write(PATH, self.new), self.deadline], [tool("read_file",path=PATH), self.deadline]])
        self.assertEqual(detail["run"]["status"], "failed")
        self.assertTrue(reports[2]["interrupted_repair"]["resume"])
        self.assertFalse(reports[3]["interrupted_repair"]["resume"])
        self.assertIn("证据均无变化", reports[3]["native_review"]["host_issue"])
        self.assertEqual(public, self.old)

    def test_unrelated_failed_checks_and_noop_passes_cannot_reopen_the_same_checkpoint(self):
        _, reports, _ = self.execute([
            [write(PATH, self.new), self.deadline],
            [tool("run_python", code="assert 1 == 2"), tool("run_python", code="assert 3 == 3"), self.deadline]])
        self.assertFalse(reports[-1]["interrupted_repair"]["resume"])
        self.assertTrue(reports[-1]["evidence"]["observed_failure"])

    def test_restore_interrupted_private_revision_without_publishing_its_unsigned_bytes(self):
        root = make_temp_dir("interrupted-private-candidate")
        self.addCleanup(cleanup, root)
        project = root / "project"
        project.mkdir()
        (project / "records.json").write_text(RECORDS)
        service = StudioService(root / "runs", Settings(api_key="test-credential"))
        self.addCleanup(service.close)
        steps = initial() + [submit_private(OLD,False), {"text":"The old destination needs changes."},
                tool("delegate_task",assignee="builder",objective="Correct it."), write(PATH,NEW), self.deadline,
                tool("delegate_task",assignee="builder",objective="Finish the interrupted revision."),
                tool("read_file",path=PATH), {"text":"Revision ready."},
                tool("delegate_task",assignee="inspector",objective="Recheck the revision."),
                tool("read_file",path=PATH), submit_private(NEW), {"text":"Rechecked."}, {"text":"Delivered."}]
        provider = TypedFailureProvider(steps)
        with patch("miniagent.studio_service.DeepSeekProvider",return_value=provider):
            run = service.start(request("team",conversation=roster("lead","builder","inspector"),
                objective="Develop and independently verify the shipping label.", workspace_path=str(project),
                acceptance_files=[PATH],limits={"max_seconds":3600}))
            detail = finished(service,run["id"])
        reports = [event["handoff"] for event in detail["room_events"] if event["kind"] == "member_report"]
        self.assertEqual(detail["run"]["status"],"completed", [(r["role"],r["remaining_issue"]) for r in reports])
        self.assertFalse(any(r["published"] for r in reports[:-1]))
        self.assertEqual(reports[1]["native_review"]["candidate_version"], reports[2]["native_review"]["candidate_version"])
        self.assertTrue(reports[2]["interrupted_repair"]["resume"])
        self.assertEqual(reports[-1]["promoted_candidate"]["source_task"], reports[-2]["task_id"])
        self.assertEqual((project/PATH).read_text(),NEW)

    def test_reserves_reviewer_and_closeout_even_with_an_unpublished_change(self):
        _, reports, public = self.execute([[write(PATH,self.new), self.deadline]], remaining=95)
        self.assertFalse(reports[-1]["interrupted_repair"]["resume"])
        self.assertIn("剩余额度不足", reports[-1]["native_review"]["host_issue"])
        self.assertEqual(public, self.old)

    def test_authorization_failure_and_user_cancellation_cannot_resume(self):
        _, reports, public = self.execute([[write(PATH,self.new), {"provider_failure":"http_error"}]])
        self.assertNotIn("interrupted_repair", reports[-1])
        self.assertEqual(reports[-1]["native_review"]["state"], "blocked")
        self.assertEqual(public, self.old)
        detail, reports, public = self.execute([[write(PATH,self.new), self.deadline]], cancel=True)
        self.assertEqual(detail["run"]["status"], "cancelled")
        self.assertIn("取消", reports[-1]["interrupted_repair"]["reason"])
        self.assertEqual(public, self.old)
