"""A valid signed review closes from its receipts, without a blind text encore."""
import hashlib
import json
import re
import unittest
from collections import Counter
from contextlib import nullcontext
from unittest.mock import patch

from miniagent.agent import COORDINATOR_PROMPT
from miniagent.config import Settings
from miniagent.providers import ScriptedProvider
from miniagent.research import Page
from miniagent.studio_reviews import digest
from miniagent.studio_service import StudioService
from miniagent.tools import ToolRegistry
from tests.support import cleanup, make_temp_dir
from tests.test_studio_conversations import tool
from tests.test_studio_crew import roster, write
from tests.test_studio_service import finished, request


PATH = "delivery.txt"
LABEL = "Shipment destination: Central warehouse. Confirmed issuer dispatch record."
INDEX_URL = "https://issuer.example/index"
SOURCE_URL = "https://issuer.example/original-dispatch"
RULE = {"id": "source", "method": "source", "description": "Verify the destination against the issuer's original dispatch."}
REASON = ("The public index returned an access gate. The original dispatch confirms Central warehouse; "
          "the index itself remains inaccessible. This conclusion relies on the original dispatch.")
BLIND_BLOCK = "[BLOCKED] I have no personal http_get receipts in this turn, so the source has not been verified."


class RoleProvider:
    """Independent role queues prevent a skipped member reply leaking to lead."""
    name = "scripted"
    transport = "scripted"
    model = "scripted-test"

    def __init__(self, scripts):
        self.providers = {role: ScriptedProvider(script) for role, script in scripts.items()}
        self.requests = []
        self.calls = Counter()

    def chat(self, messages, tools=None, temperature=0):
        system = next(m["content"] for m in messages if m["role"] == "system")
        match = re.search(r"你是「([^」]+)」", system)
        role = "lead" if system == COORDINATOR_PROMPT else match[1] if match else None
        if role not in self.providers:
            raise AssertionError("Missing an authenticated role in the system prompt")
        self.requests.append((role, messages.copy(), sorted(t.name for t in tools or [])))
        self.calls[role] += 1
        return self.providers[role].chat(messages, tools, temperature)


class SignedReviewHandoffTest(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir("signed-review-handoff")
        self.service = StudioService(self.root / "runs", Settings(api_key="test-only"))
        self.addCleanup(cleanup, self.root)
        self.addCleanup(self.service.close)

    def execute(self, *, missing=False, after_submit=None, user_acceptance=False, cancel=False):
        candidate = digest({PATH: hashlib.sha256(LABEL.encode()).hexdigest()})
        signed = tool("submit_review", candidate_version=candidate, results=[{
            "criterion_id": "source", "verdict": "passed", "reason": REASON,
            "evidence": ["file:" + PATH, "source:" + INDEX_URL, "source:" + SOURCE_URL],
        }])
        if after_submit:
            signed["tool_calls"].extend(after_submit["tool_calls"])
        blocked = missing or after_submit or user_acceptance or cancel
        delegation = tool("delegate_task", assignee="inspector", objective="Independently verify the destination.",
                          remaining_roles=[], review={"candidate": "builder", "criteria": [RULE]})
        if missing:
            delegation["tool_calls"][0]["arguments"]["deliverable_paths"] = ["required-review.md"]
        provider = RoleProvider({
            "lead": [
                tool("delegate_task", assignee="builder", objective="Publish the destination label.",
                     deliverable_paths=[PATH], remaining_roles=["inspector"]),
                delegation,
                {"text": "[BLOCKED] The assigned reviewer still has a concrete unmet obligation." if blocked else
                          "Delivered the source-verified destination label."},
            ],
            "builder": [write(PATH, LABEL), {"text": "Published the destination label."}],
            "inspector": [tool("http_get", url=INDEX_URL), tool("http_get", url=SOURCE_URL),
                          tool("read_file", path=PATH), signed, {"text": BLIND_BLOCK}],
        })
        def fetch(url, *args, **kwargs):
            body = "Request access to the public index." if url == INDEX_URL else LABEL
            return Page(url, "text/plain", body.encode(), False)

        original_dispatch = ToolRegistry.dispatch
        def cancel_after_sign(registry, name, arguments, ctx):
            result = original_dispatch(registry, name, arguments, ctx)
            if name == "submit_review" and result.ok:
                run_id = ctx.sandbox.root.relative_to(self.root / "runs").parts[0]
                self.service.cancel(run_id)
            return result

        options = {"child_checks": {"inspector": {"checks": [
            {"kind": "contains", "path": PATH, "text": "USER-REQUIRED-CERTIFICATE"}]}}} if user_acceptance else {}
        cancel_patch = patch.object(ToolRegistry, "dispatch", cancel_after_sign) if cancel else nullcontext()
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider), patch(
                "miniagent.research.fetch", side_effect=fetch), cancel_patch:
            run = self.service.start(request(
                "team", conversation=roster("lead", "builder", "inspector"),
                objective="Deliver a destination label verified against the issuer's original source.",
                acceptance_files=[PATH], required_source_review_paths=[PATH], limits={"max_steps": 80}, **options))
            detail = finished(self.service, run["id"])
        reports = [e["handoff"] for e in detail["room_events"] if e["kind"] == "member_report"]
        trace = [json.loads(line) for line in (self.root / "runs" / run["id"] / "trace.jsonl").read_text().splitlines()]
        return detail, reports, provider, trace

    def test_signed_source_review_finishes_without_requesting_a_blind_final_denial(self):
        detail, reports, provider, trace = self.execute()
        self.assertEqual(detail["run"]["status"], "completed", [(r["role"], r["remaining_issue"]) for r in reports])
        self.assertEqual(provider.calls["inspector"], 4, "No model request may follow the valid signed finding.")
        self.assertEqual(provider.providers["inspector"].cursor, 4)
        self.assertEqual(provider.providers["inspector"].script[4]["text"], BLIND_BLOCK)
        reviewer = next(r for r in reports if r["role"] == "inspector")
        self.assertTrue(reviewer["published"])
        self.assertEqual(reviewer["native_review"]["state"], "passed")
        finding, = reviewer["native_review"]["assessment"]["results"]
        signed = reviewer["signed_handoff"]
        self.assertEqual(signed["source"], "signed_review")
        self.assertEqual(signed["assessment"], reviewer["native_review"]["assessment"])
        self.assertEqual(signed["candidate_version"], reviewer["native_review"]["candidate_version"])
        self.assertEqual(signed["completion_step"], 4)
        self.assertEqual(finding["reason"], REASON)
        self.assertIn(REASON, reviewer["summary"], "Host handoff must preserve source limitations, not paraphrase them away.")
        calls = {r["tool_call_id"]: r for r in trace if r["kind"] == "tool_result"
                 and r["task_id"] == reviewer["task_id"]}
        self.assertEqual({r["kind"] for r in finding["receipts"]}, {"read", "source"})
        for receipt in finding["receipts"]:
            actual = calls[receipt["tool_call_id"]]
            self.assertTrue(actual["ok"])
            self.assertEqual(actual["tool"], "http_get" if receipt["kind"] == "source" else "read_file")
        self.assertEqual({r["url"] for r in finding["receipts"] if r["kind"] == "source"}, {INDEX_URL, SOURCE_URL})
        self.assertNotIn(BLIND_BLOCK, reviewer["summary"])
        completion, = [r for r in trace if r["kind"] == "tool_completion" and r["task_id"] == reviewer["task_id"]]
        self.assertEqual(completion["evidence"], signed)
        self.assertEqual(completion["answer"], reviewer["summary"])
        self.assertEqual([calls[call]["tool"] for call in signed["trigger_calls"]], ["submit_review"])

    def test_signed_review_does_not_complete_with_a_missing_required_attachment(self):
        detail, reports, provider, _ = self.execute(missing=True)
        reviewer = next(r for r in reports if r["role"] == "inspector")
        self.assertNotEqual(detail["run"]["status"], "completed")
        self.assertFalse(reviewer["published"])
        self.assertIn("required-review.md", reviewer["evidence"]["missing_required_files"])
        self.assertGreater(provider.calls["inspector"], 4)

    def test_failure_after_signing_in_the_same_batch_blocks_automatic_completion(self):
        detail, reports, provider, trace = self.execute(
            after_submit=tool("run_python", code="assert 1 == 2, 'new independent failure after signing'"))
        reviewer = next(r for r in reports if r["role"] == "inspector")
        self.assertNotEqual(detail["run"]["status"], "completed")
        self.assertFalse(reviewer["published"])
        self.assertTrue(reviewer["evidence"]["failed_check_details"])
        self.assertGreater(provider.calls["inspector"], 4)
        signed, = [r for r in trace if r["kind"] == "tool_result" and r["task_id"] == reviewer["task_id"]
                   and r["tool"] == "submit_review"]
        self.assertTrue(signed["ok"], "The negative case must genuinely sign before the later failure.")

    def test_candidate_sha_change_after_signing_blocks_automatic_completion(self):
        detail, reports, _, _ = self.execute(after_submit=tool(
            "run_python", code="from pathlib import Path\nPath('delivery.txt').write_text('Altered after signing')"))
        reviewer = next(r for r in reports if r["role"] == "inspector")
        self.assertNotEqual(detail["run"]["status"], "completed")
        self.assertFalse(reviewer["published"])
        self.assertIsNone(reviewer["native_review"]["assessment"])
        public = self.root / "runs" / detail["run"]["id"] / "agents" / "lead" / PATH
        self.assertEqual(public.read_text(), LABEL)

    def test_signed_review_does_not_bypass_explicit_member_acceptance(self):
        detail, reports, _, _ = self.execute(user_acceptance=True)
        reviewer = next(r for r in reports if r["role"] == "inspector")
        self.assertNotEqual(detail["run"]["status"], "completed")
        self.assertFalse(reviewer["published"])
        self.assertNotEqual(reviewer["native_review"]["state"], "passed")

    def test_cancellation_after_signing_wins_over_automatic_completion(self):
        detail, reports, _, trace = self.execute(cancel=True)
        reviewer = next(r for r in reports if r["role"] == "inspector")
        self.assertNotEqual(detail["run"]["status"], "completed")
        self.assertFalse(reviewer["published"])
        self.assertTrue(any(r["kind"] == "cancel_observed" and r["task_id"] == reviewer["task_id"] for r in trace))


if __name__ == "__main__":
    unittest.main()
