"""Unsigned review retries compare candidate and evidence, not the reviewer's prose."""
import hashlib
import json
import unittest
from copy import deepcopy
from unittest.mock import Mock, patch

from miniagent.config import Settings
from miniagent.crew_progress import _check_key, _script_inputs
from miniagent.rao import Observation, StepRecord
from miniagent.studio_reviews import Review, ReviewSession, Rule, digest
from miniagent.studio_service import StudioService
from miniagent.tools import ToolRegistry
from tests.support import cleanup, make_temp_dir
from tests.test_signed_review_handoff import RoleProvider
from tests.test_studio_conversations import tool
from tests.test_studio_crew import roster, write
from tests.test_studio_service import finished, request


PATH = "index.html"
HTML = "<h1>Meeting-room bookings</h1>"
FILES = {PATH: hashlib.sha256(HTML.encode()).hexdigest()}
RULE = Rule(id="independent_recheck", method="execute", description="Verify the frozen booking page")


def finding(ref, reason="The page passed."):
    return {"criterion_id": RULE.id, "verdict": "passed", "reason": reason, "evidence": [ref]}


class ReviewRefusalTest(unittest.TestCase):
    def test_unchanged_review_hands_back_once_without_a_signature_or_another_assignment(self):
        def submit(reason):
            return tool("submit_review", candidate_version=digest(FILES), results=[finding("file:" + PATH, reason)])
        delegation = tool("delegate_task", assignee="qa", objective="Check frozen page", remaining_roles=[],
                          review={"candidate":"dev", "criteria":[RULE.model_dump(exclude_none=True)]})
        provider = RoleProvider({
            "lead":[tool("delegate_task", assignee="dev", objective="Deliver the booking page",
                         deliverable_paths=[PATH], remaining_roles=["qa"]), delegation,
                    {"text":"[BLOCKED] The execution receipt on this candidate is missing."}],
            "dev":[write(PATH, HTML), {"text":"Delivered booking page."}],
            "qa":[tool("read_file", path=PATH), submit("The page passed."),
                  submit("I now certify the exact same page."), {"text":"This model call should never occur."}]})
        root = make_temp_dir("unchanged-signature")
        self.addCleanup(cleanup, root)
        service = StudioService(root / "runs", Settings(api_key="test-only"))
        self.addCleanup(service.close)
        workspace = root/"project"
        workspace.mkdir()
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider):
            run = service.start(request("team", conversation=roster("lead", "dev", "qa"),
                objective="Deliver independently verified booking page", acceptance_files=[PATH], workspace_path=str(workspace),
                limits={"max_steps":0, "max_total_tokens":0, "max_seconds":0}))
            detail = finished(service, run["id"])
        self.assertEqual(provider.calls["qa"], 3)
        review, = [row["handoff"] for row in detail["room_events"] if row["kind"] == "member_report" and row["role"] == "qa"]
        self.assertFalse(review["published"])
        self.assertEqual(review["native_review"]["state"], "blocked")
        self.assertIsNone(review["native_review"]["assessment"])
        refusal = review["native_review"]["evidence_required"]
        self.assertEqual(refusal["candidate_version"], digest(FILES))
        self.assertEqual(refusal["issues"][0]["criterion_id"], RULE.id)
        self.assertIn("passed_check", refusal["issues"][0]["missing"])
        self.assertIn("run_browser", str(refusal["issues"][0]["next_action"]))
        last_request = [r for r in provider.requests if r[0] == "qa"][-1]
        self.assertIn("list_review_evidence", last_request[2])
        self.assertIn("run_browser", last_request[2])
        self.assertNotEqual(detail["run"]["status"], "completed")
        trace = [json.loads(line) for line in (root/"runs"/run["id"]/"trace.jsonl").read_text().splitlines()]
        handoff, = [e for e in trace if e["kind"]=="tool_handoff_blocked"]
        self.assertEqual(handoff["evidence"], refusal)
        refusals = [e["review_refusal"] for e in trace if e["kind"]=="tool_result" and "review_refusal" in e]
        self.assertEqual(refusals, [refusal, refusal])
        self.assertEqual({e["error_class"] for e in trace if e["kind"]=="tool_result" and "review_refusal" in e},
                         {"A_REVIEW_REJECTED"})

        # A deliberate follow-up can use the very same frozen candidate with
        # fresh evidence. Its author, candidate digest and the old refusal survive.
        code = "from pathlib import Path\nassert '<h1>Meeting-room' in Path('index.html').read_text()\nprint('PASS page title')"
        valid = finding("pass:" + _check_key(_script_inputs(code)[1]))
        new_review = tool("delegate_task", assignee="qa", objective="Verify the same page using the missing check.",
                          review={"candidate":PATH, "criteria":[RULE.model_dump(exclude_none=True)]})
        retry = RoleProvider({"lead":[new_review, {"text":"The published page now has an independent check."}],
                              "qa":[tool("run_python", code=code),
                                    tool("submit_review", candidate_version=digest(FILES), results=[valid])]})
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=retry):
            follow = service.start(request("team", objective="Resume on the unchanged page with an independent check.",
                conversation={"parent_run_id":run["id"], "recipient":"lead", "intent":"edit", "recover_failed":True},
                acceptance_files=[PATH]))
            resolved = finished(service, follow["id"])
        self.assertEqual(resolved["run"]["status"],"completed", resolved["diagnostics"])
        reviewed, = [e["handoff"]["native_review"] for e in resolved["room_events"]
                     if e["kind"]=="member_report" and e["role"]=="qa"]
        self.assertEqual((reviewed["state"], reviewed["candidate_version"]), ("passed", refusal["candidate_version"]))
        self.assertEqual(workspace.joinpath(PATH).read_text(), HTML)

    def session(self):
        item = Review("candidate", "dev", "qa", "Inspect page", [RULE], FILES,
                      author_requested=True, probe_product_files=dict(FILES))
        progress = Mock()
        progress.project_versions.return_value = dict(FILES)
        progress.evidence.return_value = {"diagnostic_checks":[]}
        progress.failed_checks.return_value = {}
        progress.review_evidence.return_value = {}
        return ReviewSession(item, FILES, progress, "attempt", available_tools=["run_browser"])

    def test_only_checks_bound_to_frozen_product_are_suggested_and_recovery_can_sign(self):
        session = self.session()
        stale = session._add("pass:unrelated", {"kind":"passed_check", "asserted":True,
                             "label":"Other page passed", "asserted_inputs":{"other.html":"a"*64}}, "a")
        def attempt(ref):
            return session.submit(None, {"candidate_version":session.item.version, "results":[finding(ref)]})
        first = attempt(stale["ref"])
        issue, = first.meta["issues"]
        self.assertEqual(issue["passed_checks"], [])
        self.assertIn(PATH, str(issue["next_action"]))
        def observed(step, result):
            return StepRecord(step=step, decision_kind="tool", observations=[
                Observation(step_id=str(step), source="submit_review", content=result.content, ok=False, meta=result.meta)])
        session.progress.review_evidence.return_value = {stale["legacy_ref"]:{
            key:stale[key] for key in ("kind", "asserted", "label", "asserted_inputs", "tool_call_id")}}
        session.observe(observed(1, first))
        duplicate_before_new_check = attempt(stale["legacy_ref"])
        session.progress.review_evidence.return_value["pass:page"] = {
            "kind":"passed_check", "asserted":True, "asserted_inputs":dict(FILES),
            "label":"Booking page checked", "tool_call_id":"b"}
        session.observe(observed(2, duplicate_before_new_check))
        self.assertIsNone(session.evidence_handoff, "Evidence from the same tool batch must reach the reviewer.")
        good = session._resolve("pass:page")
        # Same stale reference, but now the host has eligible proof. Show it to
        # the reviewer instead of handing back the old failure.
        second = attempt(stale["legacy_ref"])
        self.assertNotIn("已被拒签", second.content)
        self.assertEqual(second.meta["issues"][0]["passed_checks"], [{"ref":good["ref"],"label":good["label"]}])
        self.assertTrue(attempt(good["ref"]).ok)

    def test_framework_parameter_errors_have_the_same_recovery_semantics(self):
        session = self.session()
        registry = ToolRegistry([session.tool()])
        for step in (1, 2):
            result = registry.dispatch("submit_review", {"results":[finding("file:" + PATH)]}, None)
            self.assertTrue(result.meta["invalid_arguments"])
            session.observe(StepRecord(step=step, decision_kind="tool", observations=[
                Observation(step_id=str(step), source="submit_review", ok=False, content=result.content, meta=result.meta)]))
            if step == 1:
                self.assertIsNone(session.evidence_handoff)
        self.assertIn("candidate_version", session.evidence_handoff["issues"][0]["message"])
        self.assertIsNone(session.assessment)

    def test_changed_finding_or_fixed_schema_is_valid_without_new_evidence(self):
        session = self.session()
        ref = session._add("file:" + PATH, {"kind":"read", "path":PATH, "extent":8, "sha256":FILES[PATH]}, "r")
        args = {"candidate_version":session.item.version, "results":[finding(ref["ref"])]}
        invalid = deepcopy(args)
        del invalid["results"][0]["verdict"]
        self.assertFalse(session.submit(None,invalid).ok)
        self.assertFalse(session.submit(None,args).ok)
        args["results"][0].update(verdict="inconclusive", cause="verification", action="Run the browser on the frozen page.")
        self.assertTrue(session.submit(None,args).ok)
        self.assertEqual(session.assessment["verdict"],"inconclusive")
