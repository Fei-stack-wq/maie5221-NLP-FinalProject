"""Exercise a corrected named Python check through the ordinary group run and publication path."""
import hashlib
import json
import unittest
from unittest.mock import patch

from miniagent.check_provenance import shell_invocation
from miniagent.config import Settings
from miniagent.crew_progress import _check_key, _script_inputs
from miniagent.studio_reviews import digest
from miniagent.studio_service import StudioService
from tests.support import cleanup, make_temp_dir
from tests.test_studio_conversations import RecordingProvider, tool
from tests.test_studio_crew import roster, write
from tests.test_studio_service import finished, request


PRODUCT = "booking.json"
BOOKING = '{"room":"A101","minutes":45,"status":"confirmed"}\n'
CHECK = "checks/test_rooms.py"
ALIAS = "examples/test_rooms.py"
OTHER = "tests/test_bookings.py"
BROKEN = ("from pathlib import Path\n"
          "booking = json.loads(Path('booking.json').read_text())\n"
          "assert booking['room'] == 'A101'\n"
          "print('PASS assigned room')\n")
FIXED = "import json\n" + BROKEN
OTHER_CODE = ("import json\nfrom pathlib import Path\n"
              "booking = json.loads(Path('booking.json').read_text())\n"
              "assert booking['status'] == 'confirmed'\nprint('PASS booking status')\n")
PRODUCT_ASSERTION = ("from pathlib import Path\nimport json\n"
                     "actual = json.loads(Path('booking.json').read_text())\n"
                     "assert (actual['room'], actual['minutes']) == ('A101', 45)\n"
                     "print('PASS independent room and duration')\n")
UNRELATED = "assert 45 == 45\nprint('PASS constant')\n"


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def run_check(path):
    return tool("run_shell", command="python3 " + path)


class CorrectedFileCheckTest(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir("corrected-file-check")
        self.project = self.root / "local"
        self.project.mkdir()
        self.service = StudioService(self.root / "runs", Settings(api_key="test-only"))
        self.addCleanup(cleanup, self.root)
        self.addCleanup(self.service.close)
        alias = self.project / ALIAS
        alias.parent.mkdir()
        alias.write_text(FIXED)

    def author_steps(self):
        return [tool("delegate_task", assignee="builder", objective="Deliver the confirmed A101 booking.",
                     deliverable_paths=[PRODUCT], remaining_roles=["qa"]),
                write(OTHER, OTHER_CODE),
                # A filtered failing run exposes the need to execute this exact command directly.
                tool("run_shell", command="python3 " + OTHER + " 2>&1 | tail -12"),
                write(PRODUCT, BOOKING), write(CHECK, BROKEN), run_check(CHECK)]

    def fixed_author_handoff(self):
        return self.author_steps() + [
            tool("edit_file", path=CHECK, old_text="from pathlib import Path", new_text="import json\nfrom pathlib import Path"),
            run_check(CHECK), {"text":"The missing import is fixed; the booking is ready."},
            tool("delegate_task", assignee="qa", objective="Independently verify the frozen booking.")]

    def execute(self, steps):
        runs = self.service.runs_dir
        class Provider(RecordingProvider):
            def chat(inner, messages, tools=None, temperature=0):
                if inner.cursor < len(inner.script) and "assessment_for" in inner.script[inner.cursor]:
                    # Only the persisted public handoff supplies the candidate
                    # version and identities of the original failed checks.
                    journal, = runs.glob("*/public-room.jsonl")
                    events = [json.loads(line) for line in journal.read_text().splitlines()]
                    pending, = [row for row in events if row["kind"] == "author_probe_pending"]
                    script = inner.script[inner.cursor]["assessment_for"]
                    proof = "pass:" + _check_key(_script_inputs(script)[1])
                    keys = ["independent_recheck", *["probe_" + scope for scope in pending["original_calls"]]]
                    inner.script[inner.cursor] = tool("submit_review", candidate_version=pending["candidate_version"],
                        results=[{"criterion_id": key, "verdict": "passed", "evidence": [proof],
                                  "reason": "Checked the frozen booking and the originally requested tests."}
                                 for key in keys])
                return super().chat(messages, tools, temperature)

        provider = Provider(steps)
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider):
            run = self.service.start(request("team", conversation=roster("lead", "builder", "qa"),
                objective="Deliver the booking and independently verify the assigned room and duration.",
                workspace_path=str(self.project), acceptance_files=[PRODUCT], limits={"max_steps":100}))
            detail = finished(self.service, run["id"])
        reports = [row["handoff"] for row in detail["room_events"] if row["kind"] == "member_report"]
        trail = [json.loads(line) for line in (runs / run["id"] / "trace.jsonl").read_text().splitlines()]
        return detail, reports, trail

    def test_only_independent_product_assertion_and_both_direct_checks_promote_the_frozen_candidate(self):
        steps = self.fixed_author_handoff() + [
            tool("run_python", code=PRODUCT_ASSERTION), run_check(ALIAS), {"assessment_for": PRODUCT_ASSERTION},
            run_check(OTHER), {"assessment_for": PRODUCT_ASSERTION}, run_check(CHECK),
            tool("run_python", code=UNRELATED), {"assessment_for": UNRELATED}, {"assessment_for": PRODUCT_ASSERTION},
            {"text":"The two original commands and the independent product assertion succeeded."},
            {"text":"Delivered the independently verified booking."}]
        detail, reports, trail = self.execute(steps)
        self.assertEqual(detail["run"]["status"], "completed", [(r["role"], r["remaining_issue"]) for r in reports])
        author, qa = reports
        self.assertEqual((author["role"], author["published"], qa["role"], qa["published"]), ("builder", False, "qa", True))
        self.assertTrue(any(r["kind"] == "handoff_pending_peer" and r["task_id"] == author["task_id"] for r in trail))
        pending = author["author_probe_review"]
        self.assertEqual(pending["candidate_version"], digest({p:sha(v) for p, v in
                         [(PRODUCT, BOOKING), (CHECK, FIXED), (ALIAS, FIXED), (OTHER, OTHER_CODE)]}))
        corrected, = pending["corrected_scripts"]
        self.assertEqual((corrected["path"], corrected["prior_sha256"], corrected["current_sha256"]), (CHECK, sha(BROKEN), sha(FIXED)))
        self.assertEqual(corrected["scope"], shell_invocation("python3 " + CHECK, ".").scope)
        self.assertEqual(corrected["input_version"], pending["candidate_version"])
        self.assertEqual(corrected["original_calls"], pending["original_calls"])
        original_calls = set(pending["original_calls"].values())
        author_results = [r for r in trail if r["kind"] == "tool_result" and r["task_id"] == author["task_id"]]
        self.assertTrue(any(r["tool_call_id"] in original_calls and not r["ok"] and r["exit_code"] == 1
                            for r in author_results))
        self.assertTrue(any(row["tool_call_id"] in original_calls and "NameError" in row["error_excerpt"]
                            for row in author["evidence"]["failed_check_details"]))
        self.assertTrue(any(r["tool_call_id"] == corrected["tool_call_id"] and r["ok"] for r in author_results))
        self.assertIn(OTHER, [r["path"] for r in author["evidence"]["pending_verification"]])
        self.assertTrue(author["evidence"]["observed_failure"])
        submits = [r for r in trail if r["kind"] == "tool_result" and r["task_id"] == qa["task_id"]
                   and r["tool"] == "submit_review"]
        self.assertEqual([r["ok"] for r in submits], [False, False, False, True])
        self.assertIn(OTHER, submits[0]["preview"])
        self.assertIn(CHECK, submits[1]["preview"])
        self.assertEqual(qa["native_review"]["state"], "passed")
        self.assertEqual(qa["promoted_candidate"]["source_task"], author["task_id"])
        self.assertEqual((self.project / PRODUCT).read_text(), BOOKING)
        self.assertEqual((self.project / CHECK).read_text(), FIXED)

    def test_peer_rerunning_only_the_corrected_script_still_needs_the_other_original_command(self):
        steps = self.fixed_author_handoff() + [
            tool("run_python", code=PRODUCT_ASSERTION), run_check(CHECK), {"assessment_for": PRODUCT_ASSERTION},
            {"text":"[BLOCKED] The other original command has not run."},
            {"text":"[BLOCKED] Independent verification is incomplete."}]
        detail, reports, trail = self.execute(steps)
        self.assertNotEqual(detail["run"]["status"], "completed")
        self.assertFalse(any(r["published"] for r in reports))
        signed, = [r for r in trail if r["kind"] == "tool_result" and r["tool"] == "submit_review"]
        self.assertFalse(signed["ok"])
        self.assertIn(OTHER, signed["preview"])
        self.assertFalse((self.project / PRODUCT).exists())

    def test_named_failure_and_noisy_custom_reporter_reach_independent_adjudication(self):
        historical = ("import json, sys\nfrom pathlib import Path\n"
                      "minutes = json.loads(Path('booking.json').read_text())['minutes']\n"
                      "print('http access ' * 420, file=sys.stderr)\n"
                      "try:\n    assert minutes == 60\nexcept AssertionError:\n"
                      "    print('FAIL  allocated reservation duration')\n    sys.exit(1)\n"
                      "print('PASSED 1/1')\nprint('ALL TESTS PASSED')\n")
        corrected = historical.replace("minutes == 60", "minutes == 45")
        steps = [tool("delegate_task", assignee="builder", objective="Deliver the confirmed booking.",
                      deliverable_paths=[PRODUCT], remaining_roles=["qa"]),
                 write(PRODUCT, BOOKING), write(CHECK, historical), run_check(CHECK),
                 tool("edit_file", path=CHECK, old_text="minutes == 60", new_text="minutes == 45"),
                 run_check(CHECK), {"text":"The assigned booking is ready for independent verification."},
                 tool("delegate_task", assignee="qa", objective="Verify the disputed reservation duration."),
                 run_check(CHECK), tool("run_python", code=UNRELATED), {"assessment_for": UNRELATED},
                 tool("run_python", code=PRODUCT_ASSERTION), {"assessment_for": PRODUCT_ASSERTION},
                 {"text":"Independently verified the reservation duration."}, {"text":"Delivered the booking."}]
        detail, reports, trail = self.execute(steps)
        self.assertEqual(detail["run"]["status"], "completed", [(r["role"], r["remaining_issue"]) for r in reports])
        author, qa = reports
        self.assertFalse(author["published"])
        original, = author["evidence"]["failed_check_details"]
        self.assertEqual(original["label"], "工具输出 FAIL · allocated reservation duration")
        receipt, = author["author_probe_review"]["corrected_scripts"]
        self.assertEqual(receipt["original_calls"], {original["scope_id"]: original["tool_call_id"]})
        self.assertEqual((receipt["prior_sha256"], receipt["current_sha256"]), (sha(historical), sha(corrected)))
        self.assertFalse(receipt["asserted"])
        self.assertTrue(receipt["output_truncated"])
        self.assertNotEqual(original["tool_call_id"], receipt["tool_call_id"])
        self.assertEqual([r["ok"] for r in trail if r["kind"] == "tool_result" and r["tool"] == "submit_review"],
                         [False, True])
        self.assertEqual((qa["role"], qa["native_review"]["state"], qa["published"]), ("qa", "passed", True))

    def test_passing_an_alias_or_removing_the_assertion_does_not_earn_a_private_handoff(self):
        for mode in ("same basename", "removed assertion"):
            with self.subTest(mode=mode):
                if mode == "same basename":
                    attempt = [run_check(ALIAS)]
                else:
                    attempt = [write(CHECK, FIXED.replace("assert booking['room'] == 'A101'\n", "")), run_check(CHECK)]
                steps = self.author_steps() + attempt + [{"text":"The command printed PASS."},
                    {"text":"[BLOCKED] No matching corrected assertion."}, {"text":"[BLOCKED] Booking unverified."}]
                detail, reports, trail = self.execute(steps)
                self.assertTrue(reports)
                self.assertFalse(reports[0]["published"])
                self.assertTrue(reports[0]["evidence"]["observed_failure"])
                self.assertFalse(any(r["kind"] == "handoff_pending_peer" for r in trail))
                self.assertFalse(any("author_probe_review" in r for r in reports))
                self.assertNotEqual(detail["run"]["status"], "completed")
                self.assertFalse((self.project / PRODUCT).exists())
