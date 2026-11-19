import hashlib
import json
import unittest
from unittest.mock import Mock, patch

from miniagent.config import Settings
from miniagent.studio_reviews import Review, ReviewSession, Rule, digest
from miniagent.studio_service import StudioService
from tests.support import cleanup, make_temp_dir
from tests.test_studio_conversations import RecordingProvider, tool
from tests.test_studio_crew import roster, write
from tests.test_studio_service import finished, request


RULE = {"id": "quantity", "method": "execute", "description": "Verify the quantity on the shipment label."}
PATH = "label.txt"
LABEL = "Verified shipment quantity: 12 units."
SHA = hashlib.sha256(LABEL.encode()).hexdigest()


def submission(ref):
    return tool("submit_review", candidate_version=digest({PATH: SHA}), results=[{
        "criterion_id": "quantity", "verdict": "passed", "reason": "Checked the quantity on the actual label.",
        "evidence": [ref]}])


class UseCorrection(RecordingProvider):
    def chat(self, messages, tools=None, temperature=0):
        if self.cursor < len(self.script) and self.script[self.cursor].get("use_correction"):
            for message in reversed(messages):
                content = message.get("content") or ""
                if message.get("role") != "tool" or not isinstance(content, str):
                    continue
                start = content.find('[{"criterion_id"')
                if start < 0:
                    continue
                issues, _ = json.JSONDecoder().raw_decode(content[start:])
                if checks := issues[0].get("passed_checks"):
                    self.script[self.cursor] = submission(checks[0]["ref"])
                    break
        return super().chat(messages, tools, temperature)


class ExecuteReviewGuidanceTest(unittest.TestCase):
    def test_script_exit_and_wrong_sha_recover_with_a_real_assertion_and_its_ref(self):
        root = make_temp_dir("execute-ref")
        self.addCleanup(cleanup, root)
        service = StudioService(root / "runs", Settings(api_key="test-credential"))
        self.addCleanup(service.close)
        condition = "from pathlib import Path\nactual = Path('label.txt').read_text()\n"
        steps = [tool("delegate_task", assignee="writer", objective="Write the shipment label.",
                      deliverable_paths=[PATH], remaining_roles=["reviewer"]),
                 write(PATH, LABEL), {"text": "Label ready."},
                 tool("delegate_task", assignee="reviewer", objective="Check the actual quantity.",
                      review={"candidate": "writer", "criteria": [RULE]}),
                 tool("list_review_evidence"),
                 write("quantity_check.py", condition + 'import sys\nprint(actual)\nsys.exit(0 if "12 units" in actual else 1)\n'),
                 tool("run_shell", command="python3 quantity_check.py"), submission(SHA),
                 tool("run_python", code=condition + 'assert "12 units" in actual\nprint("PASS shipment quantity")\n'),
                 submission(SHA), {"use_correction": True}, {"text": "Shipment label verified."}]
        provider = UseCorrection(steps)
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider):
            job = service.start(request("team", conversation=roster("lead", "writer", "reviewer"),
                objective="Deliver the quantity label after an independent executed check.",
                acceptance_files=[PATH], limits={"max_steps": 90}))
            detail = finished(service, job["id"])
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        catalog = next(json.loads(msg["content"]) for msg in provider.requests[5][0] if
                       msg.get("role") == "tool" and '"candidate_version"' in msg.get("content", "")
                       and '"evidence": []' in msg["content"])
        self.assertEqual(catalog["total"], 0)
        actions = catalog["execute"]["actions"]
        self.assertIn("顶层 assert", actions["run_python"])
        self.assertIn("expect", actions["run_browser"])
        self.assertEqual(set(actions), {option["tool"] for option in catalog["needs"]["quantity"]})
        correction = str(provider.requests[8][0])
        self.assertIn('"passed_checks": []', correction)
        self.assertIn('尚无可引用的 passed_check', correction)
        review = next(event["handoff"]["native_review"] for event in detail["room_events"]
                      if event["kind"] == "member_report" and event["role"] == "reviewer")
        finding, = review["assessment"]["results"]
        receipt, = finding["receipts"]
        self.assertEqual((receipt["kind"], receipt["asserted_inputs"]), ("passed_check", {PATH: SHA}))
        self.assertEqual(finding["evidence"], [receipt["ref"]])
        self.assertRegex(receipt["ref"], r"^E[0-9a-f]{16}$")

    def test_closing_offers_only_the_roles_registered_execution_tools(self):
        files = {PATH: SHA}
        rule = Rule.model_validate(RULE)
        for allowed in (("run_python",), ()):
            with self.subTest(allowed=allowed):
                review = Review("target", "writer", "reviewer", "Check the quantity.", [rule], files)
                session = ReviewSession(review, files, Mock(), "attempt", available_tools=allowed)
                self.assertEqual(set(session.catalog()["execute"]["actions"]), set(allowed))
                self.assertEqual(session.closing_options(), {"submit_review", *allowed})


if __name__ == "__main__":
    unittest.main()
