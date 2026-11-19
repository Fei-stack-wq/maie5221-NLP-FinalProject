"""A review-only continuation binds the previous author's real public bytes."""
import hashlib
import json
import unittest
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.crew_progress import _check_key, _script_inputs
from miniagent.research import Page
from miniagent.studio_reviews import digest
from miniagent.studio_service import StudioService
from tests.support import cleanup, make_temp_dir
from tests.test_studio_conversations import RecordingProvider, reply, tool
from tests.test_studio_crew import roster, write
from tests.test_studio_service import finished, request

FILE = "dispatch.md"
BODY = "Confirmed dispatch order: deliver to the North warehouse on Tuesday."
URL = "https://issuer.example/dispatch"
SHA = hashlib.sha256(BODY.encode()).hexdigest()
RULE = {"id":"issued", "method":"source", "description":"Check destination against the current issuer order."}


class InheritedReviewTest(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir("inherited-review")
        self.local = self.root / "local"
        self.local.mkdir()
        self.service = StudioService(self.root / "runs", Settings(api_key="test-credential"))
        self.addCleanup(cleanup, self.root)
        self.addCleanup(self.service.close)

    def turn(self, script, conversation, **options):
        provider = RecordingProvider(script)
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider), \
             patch("miniagent.research.fetch", return_value=Page(URL, "text/plain", BODY.encode(), False)):
            run = self.service.start(request("team", conversation=conversation, objective="Verify the current dispatch.",
                acceptance_files=[FILE], limits={"max_steps":80}, **options))
            return finished(self.service, run["id"]), provider

    def seed(self, *, local=True, blocked=False):
        steps = [tool("delegate_task", assignee="writer", objective="Write the dispatch.", deliverable_paths=[FILE],
                      remaining_roles=[]), write(FILE, BODY), {"text":"Published the dispatch."},
                 {"text":"[BLOCKED] Separate publication hold." if blocked else "Delivered."}]
        detail, _ = self.turn(steps, roster("lead", "writer", "checker"), **({"workspace_path":str(self.local)} if local else {}))
        handoff, = [e for e in detail["room_events"] if e["kind"] == "member_report"]
        self.assertTrue(handoff["published"])
        return detail["run"]["id"], handoff["task_id"]

    def review(self, candidate="writer", assignee="checker"):
        return tool("delegate_task", assignee=assignee, objective="Check the published original.",
                    review={"candidate":candidate, "criteria":[RULE]}, remaining_roles=[])

    def submit(self):
        return tool("submit_review", candidate_version=digest({FILE:SHA}), results=[
            {"criterion_id":"issued","verdict":"passed","reason":"The dispatch and the issuer agree.",
             "evidence":["file:"+FILE, "source:"+URL]}])

    def test_published_member_in_failed_turn_can_be_reviewed_without_republishing(self):
        parent, task = self.seed(blocked=True)
        detail, provider = self.turn([self.review(assignee="writer"), self.review(),
            tool("read_lines", path=FILE), self.submit(), tool("http_get", url=URL), self.submit(),
            {"text":"Verified the published dispatch."}, {"text":"Delivered reviewed dispatch."}], reply(parent, "lead"),
            required_source_review_paths=[FILE])
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        events = detail["room_events"]
        bound, = [e for e in events if e["kind"] == "published_review_bound"]
        self.assertEqual(bound["publication"], {"run_id":parent, "task_id":task, "role":"writer", "files":{FILE:SHA}})
        self.assertEqual(bound["reviewer"], "checker")
        report, = [e["handoff"] for e in events if e["kind"] == "member_report"]
        self.assertEqual(report["native_review"]["state"], "passed")
        self.assertEqual(report["native_review"]["target"], task)
        self.assertEqual(report["native_review"]["assessment"]["results"][0]["receipts"][1]["url"], URL)
        self.assertNotIn(FILE, [c["path"] for c in report["changed_files"]])
        self.assertIn("本群其他成员", str(provider.requests))
        self.assertIn("未观察到", str(provider.requests))

    def test_internal_workspace_uses_base_run_and_supports_exact_path(self):
        parent, task = self.seed(local=False)
        detail, _ = self.turn([self.review(FILE), tool("read_file", path=FILE), tool("http_get", url=URL),
                self.submit(), {"text":"Independently verified."}, {"text":"Delivered."}],
            reply(parent, "lead"), required_source_review_paths=[FILE])
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        self.assertEqual([e["publication"]["task_id"] for e in detail["room_events"]
                          if e["kind"] == "published_review_bound"], [task])

    def test_unresolved_independent_product_failure_on_same_bytes_cannot_be_dropped_on_next_turn(self):
        parent, _ = self.seed()
        rule = {"id":"delivery_day", "method":"inspect", "description":"The confirmed dispatch day must be Friday."}
        failed = tool("submit_review", candidate_version=digest({FILE:SHA}), results=[{
            "criterion_id":"delivery_day", "verdict":"changes_requested", "reason":"The file says Tuesday.",
            "evidence":["file:"+FILE], "cause":"product", "action":"Correct the dispatch day."}])
        prior, _ = self.turn([tool("delegate_task",assignee="checker",objective="Verify dispatch day.",
                review={"candidate":FILE, "criteria":[rule]}, remaining_roles=[]), tool("read_file", path=FILE),
            failed, {"text":"The delivery day is incorrect."}, {"text":"[BLOCKED] Awaiting the correction."}],
            reply(parent,"lead"))
        report, = [e["handoff"] for e in prior["room_events"] if e["kind"] == "member_report"]
        self.assertEqual(report["native_review"]["state"], "repair")
        last, provider = self.turn([self.review(FILE), {"text":"[BLOCKED] Awaiting the corrected version."}],
                                   reply(prior["run"]["id"], "lead"), required_source_review_paths=[FILE])
        self.assertNotEqual(last["run"]["status"], "completed")
        self.assertNotIn("published_review_bound", [e["kind"] for e in last["room_events"]])
        self.assertIn("已有独立验收确认的产品缺陷", str(provider.requests))

    def test_intervening_file_reset_ends_the_lineage_even_when_bytes_match(self):
        parent, task = self.seed()
        reset, _ = self.turn([{"text":"[BLOCKED] No new author handoff."}],
                              {**reply(parent, "lead"), "inherit_files":False}, workspace_path=str(self.local))
        last, _ = self.turn([self.review(task), {"text":"[BLOCKED] Missing same-project publication."}],
                            reply(reset["run"]["id"], "lead"), required_source_review_paths=[FILE])
        self.assertNotEqual(last["run"]["status"], "completed")
        self.assertNotIn("published_review_bound", [e["kind"] for e in last["room_events"]])

    def test_unresolved_definitions_can_be_reused_verbatim_and_extended_without_retyping(self):
        identity = {"id":"identity", "method":"execute",
                    "description":"Verify the original dispatch hash and byte length. " + "Frozen shipment record. " * 44 + "END."}
        original = [identity, RULE]
        extra = {"id":"weekday", "method":"inspect", "description":"Check that the dispatch states the weekday.",
                 "required":False}

        def delegate(criteria=None):
            review = {"candidate":task}
            if criteria is not None:
                review["criteria"] = criteria
            return tool("delegate_task", assignee="checker", objective="Check the published dispatch.",
                        review=review, remaining_roles=[])

        def sign(results):
            return tool("submit_review", candidate_version=digest({FILE:SHA}), results=results)

        probe = (f"import hashlib\nfrom pathlib import Path\nraw = Path({FILE!r}).read_bytes()\n"
                 f"assert len(raw) == {len(BODY.encode())} and hashlib.sha256(raw).hexdigest() == {SHA!r}\n"
                 "print('PASS document identity')\n")
        passed = [{"criterion_id":"identity", "verdict":"passed", "reason":"Verified the exact bytes and digest.",
                   "evidence":["pass:"+_check_key(_script_inputs(probe)[1])]},
                  {"criterion_id":"issued", "verdict":"passed", "reason":"Compared the dispatch with the issuer.",
                   "evidence":["file:"+FILE,"source:"+URL]}]
        for additions in ([], [extra]):
            with self.subTest(additions=additions):
                self.local = self.root / ("extra" if additions else "inherit")
                self.local.mkdir()
                parent, task = self.seed()
                pending, _ = self.turn([delegate(original), tool("read_file", path=FILE), sign([
                    {"criterion_id":r["id"], "verdict":"inconclusive", "cause":"verification", "action":"Acquire independent evidence.",
                     "reason":"Only the original file has been read.", "evidence":["file:"+FILE]} for r in original]),
                    {"text":"[BLOCKED] Missing executed check and independent issuer source."},
                    {"text":"[BLOCKED] The original criteria remain open."}], reply(parent, "lead"), required_source_review_paths=[FILE])
                report = next(e["handoff"] for e in pending["room_events"] if e["kind"] == "member_report")
                self.assertEqual(report["native_review"]["assessment"]["verdict"], "inconclusive")
                prior = pending["run"]["id"]
                rewritten = [{**identity, "method":"inspect", "description":"Read the file name only."}, *additions]
                results = passed + ([{"criterion_id":"weekday", "verdict":"passed", "reason":"The file specifies Tuesday.",
                                      "evidence":["file:"+FILE]}] if additions else [])
                steps = [delegate(rewritten), delegate(additions or None), tool("read_file", path=FILE),
                         tool("http_get", url=URL), tool("run_python", code=probe), sign(results),
                         {"text":"Verified the published dispatch."}, {"text":"Delivered the independently checked dispatch."}]
                detail, provider = self.turn(steps, reply(prior, "lead"), required_source_review_paths=[FILE])
                self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
                feedback = next(m["content"] for m in provider.requests[1][0]
                                if "宿主会原样补齐" in str(m.get("content")) and "END." in str(m.get("content")))
                canonical, _ = json.JSONDecoder().raw_decode("[" + feedback.split("原定义：[", 1)[1])
                self.assertEqual(canonical, [{**r,"required":True} for r in original])
                bound, = [e for e in detail["room_events"] if e["kind"] == "published_review_bound"]
                self.assertEqual(bound["publication"]["task_id"], task)
                self.assertEqual(bound["criteria"], [*additions, *canonical])
                signed = next(e["handoff"]["native_review"]["assessment"] for e in detail["room_events"]
                              if e["kind"] == "member_report")
                self.assertEqual({f["criterion_id"] for f in signed["results"]}, {r["id"] for r in bound["criteria"]})

    def test_no_prior_criteria_cannot_create_an_empty_review(self):
        parent, task = self.seed()
        detail, provider = self.turn([tool("delegate_task", assignee="checker", objective="Check the original.",
                                      review={"candidate":task}), {"text":"[BLOCKED] No criteria supplied."}],
                                     reply(parent, "lead"), required_source_review_paths=[FILE])
        self.assertIn("请在 review.criteria 提供至少一项必需标准", str(provider.requests))
        self.assertNotIn("published_review_bound", [e["kind"] for e in detail["room_events"]])

    def test_current_writer_cannot_review_older_publication_after_restoring_identical_bytes(self):
        parent, task = self.seed()
        steps = [tool("delegate_task", assignee="writer", objective="Update the dispatch.", deliverable_paths=[FILE],
                      remaining_roles=["checker"]), write(FILE, BODY+" Draft."), {"text":"Draft delivered."},
                 tool("delegate_task", assignee="checker", objective="Restore the approved original.", deliverable_paths=[FILE]),
                 write(FILE, BODY), {"text":"Restored original contents."}, self.review(task, assignee="checker"),
                 {"text":"[BLOCKED] Current author requires an independent reviewer."}]
        detail, _ = self.turn(steps, reply(parent, "lead"), required_source_review_paths=[FILE])
        changed = [e["handoff"]["changed_files"] for e in detail["room_events"] if e["kind"] == "member_report"]
        self.assertEqual(changed[-1][0]["sha256"], SHA)
        self.assertNotIn("published_review_bound", [e["kind"] for e in detail["room_events"]])
        self.assertNotEqual(detail["run"]["status"], "completed")

    def test_current_hard_acceptance_still_applies_after_independent_success(self):
        parent, _ = self.seed()
        detail, _ = self.turn([self.review(FILE), tool("read_file", path=FILE), tool("http_get", url=URL), self.submit(),
                {"text":"The issuer and document agree."}, {"text":"[BLOCKED] Delivery gate also requires Friday."}],
            reply(parent, "lead"), required_source_review_paths=[FILE],
            acceptance_checks=[{"kind":"contains","path":FILE,"text":"Friday"}])
        report, = [e["handoff"] for e in detail["room_events"] if e["kind"] == "member_report"]
        self.assertEqual(report["native_review"]["state"], "passed")
        self.assertNotEqual(detail["run"]["status"], "completed")

    def test_changed_workspace_missing_snapshot_or_unrelated_room_cannot_borrow_provenance(self):
        for mode in ("changed", "missing_archive", "other_room", "reset"):
            with self.subTest(mode=mode):
                self.local.joinpath(FILE).unlink(missing_ok=True)
                parent, task = self.seed()
                conversation = reply(parent, "lead")
                if mode == "changed":
                    self.local.joinpath(FILE).write_text(BODY + " An external change.")
                if mode == "missing_archive":
                    next(self.service.runs_dir.joinpath(parent).glob("handoffs/*/project/dispatch.md")).unlink()
                if mode == "other_room":
                    conversation = roster("lead", "writer", "checker")
                if mode == "reset":
                    conversation["inherit_files"] = False
                detail, _ = self.turn([self.review(task), {"text":"[BLOCKED] No published candidate in this workspace."}],
                    conversation, workspace_path=str(self.local), required_source_review_paths=[FILE])
                self.assertNotEqual(detail["run"]["status"], "completed")
                self.assertNotIn("published_review_bound", [e["kind"] for e in detail["room_events"]])
