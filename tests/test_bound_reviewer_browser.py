"""A real local browser can resolve an executable dispute on frozen input."""
import ast
import hashlib
import json
import unittest
from unittest.mock import patch

from miniagent.crew_progress import _check_key
from tests import test_bound_reviewer_probe as fixture
from tests.test_studio_conversations import tool


CODE = ("from pathlib import Path\n"
        "page.goto(Path('delivery.txt').resolve().as_uri())\n"
        "expect(page.locator('body')).to_contain_text('quantity: 10 units.')\n")


def browser_scope(code):
    suite = hashlib.sha256((".\0" + ast.dump(ast.parse(code))).encode()).hexdigest()
    return _check_key("browser " + suite)


class BoundBrowserProbeTest(unittest.TestCase):
    setUp = fixture.BoundReviewerProbeTest.setUp
    run_steps = fixture.BoundReviewerProbeTest.run_steps

    def steps(self, code):
        return fixture.BoundReviewerProbeTest.start_bound_review() + [
            tool("delegate_task", assignee="arbiter", objective="Resolve the original frozen-product dispute in a real browser."),
            tool("read_file", path=fixture.PATH), tool("run_browser", code=code),
            {"_probe_finding": {"criterion_id":"probe_" + fixture.BAD_SCOPE, "verdict":"passed",
                "reason":"The original inspector added ten. The real browser displays the original ten-unit quantity on the frozen product.",
                "evidence":["file:" + fixture.PATH, "pass:" + browser_scope(code)]}},
        ]

    def test_signed_browser_verification_gap_returns_to_fresh_arbiter_after_blocked_handoff(self):
        bad = CODE.replace("quantity: 10 units.", "quantity: 99 units.").replace(
            "to_contain_text('quantity: 99 units.')", "to_contain_text('quantity: 99 units.', timeout=200)")
        first = self.steps(CODE)
        first.insert(-1, tool("run_browser", code=bad))
        finding = first[-1]["_probe_finding"]
        finding.update(verdict="inconclusive", cause="verification",
                       action="Recheck the expected count in the original frozen document.",
                       evidence=["fail:" + browser_scope(bad), "pass:" + browser_scope(CODE)])
        blocked = "[BLOCKED] I cannot resolve the differing quantities with my current measurement."
        second = self.steps(CODE)[-4:]
        steps = first + [{"text": blocked}] + second + [{"text": "Verified the original ten units."},
                                                        {"text": "Delivered the independently checked label."}]
        detail, reports, provider = self.run_steps(steps)
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        first_report, final_report = [r for r in reports if r.get("native_review", {}).get("kind") == "reviewer_probe"]
        self.assertFalse(first_report["published"])
        self.assertEqual(first_report["status"], "error")
        self.assertEqual(first_report["native_review"]["state"], "review")
        self.assertEqual(final_report["native_review"]["state"], "passed")
        self.assertEqual(first_report["native_review"]["candidate_version"], final_report["native_review"]["candidate_version"])
        self.assertTrue(first_report["evidence"]["replayable_browser_call_ids"])
        trace = [json.loads(l) for l in (self.root / "runs" / detail["run"]["id"] / "trace.jsonl").read_text().splitlines()]
        member = [e for e in trace if e.get("task_id") == first_report["task_id"]]
        signed = next(i for i, e in enumerate(member) if e["kind"] == "tool_result" and e.get("ok") and e["tool"] == "submit_review")
        self.assertEqual([e["offered_tools"] for e in member[signed+1:] if e["kind"] == "step_start"], [[]])

    def test_real_browser_expectation_on_frozen_entrypoint_can_close_original_scope(self):
        steps = self.steps(CODE) + [{"text":"Independently resolved the original checker error."},
                                   {"text":"Delivered the independently checked label."}]
        detail, reports, _ = self.run_steps(steps)
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        finding = reports[-1]["native_review"]["assessment"]["results"][0]
        check, = [r for r in finding["receipts"] if r["kind"] == "passed_check"]
        self.assertIn(fixture.PATH, check["asserted_inputs"])

    def test_unrelated_page_assertion_cannot_resolve_frozen_product_dispute(self):
        code = CODE.replace("expect(page.locator", "page.set_content('<body>quantity: 10 units.</body>')\nexpect(page.locator")
        steps = self.steps(code) + [{"text":"[BLOCKED] The successful browser assertion checked a replacement page."},
                                   {"text":"[BLOCKED] No independent product execution receipt."}]
        detail, reports, _ = self.run_steps(steps)
        self.assertNotEqual(detail["run"]["status"], "completed")
        self.assertFalse(reports[-1]["published"])
        self.assertIsNone(reports[-1]["native_review"]["assessment"])

    def test_external_link_invalidates_attribution_until_explicit_return_to_original(self):
        label = '<html><body>Shipment quantity: 10 units.<a href="about:blank">External</a></body></html>'
        for reanchor in (False, True):
            with self.subTest(reanchor=reanchor):
                code = ("from pathlib import Path\n"
                        "page.goto(Path('index.html').resolve().as_uri())\n"
                        "page.locator('a').click()\n")
                code += ("page.goto(Path('index.html').resolve().as_uri())\n"
                         "expect(page.locator('body')).to_contain_text('quantity: 10 units.')\n" if reanchor else
                         "expect(page).to_have_url('about:blank')\n")
                steps = json.loads(json.dumps(fixture.BoundReviewerProbeTest.start_bound_review(label=label))
                                   .replace("delivery.txt", "index.html"))
                steps[-2]["tool_calls"][0]["arguments"]["candidate_version"] = fixture.digest({
                    "index.html": hashlib.sha256(label.encode()).hexdigest()})
                steps += [tool("delegate_task", assignee="arbiter", objective="Resolve the original product dispute."),
                    tool("read_file", path="index.html"), tool("run_browser", code=code),
                    {"_probe_finding":{"criterion_id":"probe_" + fixture.BAD_SCOPE, "verdict":"passed",
                        "reason":"Independently checking the frozen product's actual quantity, not the destination of its external link.",
                        "evidence":["file:index.html", "pass:" + browser_scope(code)]}},
                    {"text":"Independent check complete." if reanchor else "[BLOCKED] The assertion ran on an external page."},
                    {"text":"Delivered." if reanchor else "[BLOCKED] Missing a product execution receipt."}]
                provider = fixture.ProbeProvider(steps)
                with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider):
                    run = self.service.start(fixture.request("team",
                        conversation=fixture.roster("lead", "builder", "inspector", "arbiter"),
                        objective="Deliver an independently checked ten-unit label.", acceptance_files=["index.html"],
                        limits={"max_steps":90}))
                    detail = fixture.finished(self.service, run["id"])
                self.assertEqual(detail["run"]["status"] == "completed", reanchor, detail["diagnostics"])


if __name__ == "__main__":
    unittest.main()
