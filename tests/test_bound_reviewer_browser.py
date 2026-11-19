"""A real local browser can resolve an executable dispute on frozen input."""
import ast
import hashlib
import unittest

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


if __name__ == "__main__":
    unittest.main()
