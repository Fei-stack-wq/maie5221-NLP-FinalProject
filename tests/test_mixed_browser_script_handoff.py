"""A disputed browser check and a separate interrupted test can travel to the same independent reviewer."""
import hashlib
import json
import unittest
from unittest.mock import patch

from miniagent.check_provenance import shell_invocation
from miniagent.config import Settings
from miniagent.studio_reviews import digest
from miniagent.studio_service import StudioService
from tests.support import cleanup, make_temp_dir
from tests.test_bound_reviewer_browser import browser_scope
from tests.test_studio_conversations import RecordingProvider, tool
from tests.test_studio_crew import roster, write
from tests.test_studio_service import finished, request


PRODUCT = "booking.html"
SUITE = "tests/test_bookings.py"
COMMAND = "python3 tests/test_bookings.py -v"
HTML = """<!doctype html><html><title>Rooms</title><label for="room">Meeting room</label>
<select id="room"><option value="A101">A101 · 12 seats</option></select></html>"""
README = "Open booking.html and use the Meeting room selector to find A101."
TEST_CODE = ("from pathlib import Path\nimport unittest\n"
             "class Rooms(unittest.TestCase):\n"
             "    def test_page(self): self.assertIn('<select', Path('booking.html').read_text())\n"
             "if __name__ == '__main__': unittest.main()\n")
PAGE = "from pathlib import Path\npage.goto(Path('booking.html').resolve().as_uri())\n"
WRONG = (PAGE + "expect(page.get_by_label('Meeting room')).to_have_count(1)\n"
         "assumed_capacity = 8\nassert assumed_capacity == 12")
CORRECTED = PAGE + "assert page.locator('#room option:checked').inner_text() == 'A101 · 12 seats'"
INDEPENDENT = (PAGE + "selector = page.get_by_label('Meeting room')\n"
               "assert selector.input_value() == 'A101'\n"
               "assert selector.locator('option:checked').inner_text() == 'A101 · 12 seats'")


def file_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


class MixedBrowserScriptHandoffTest(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir("mixed-browser-script-review")
        self.project = self.root / "workspace"
        self.project.mkdir()
        self.service = StudioService(self.root / "runs", Settings(api_key="test-only"))
        self.addCleanup(cleanup, self.root)
        self.addCleanup(self.service.close)

    def run_room(self, steps):
        service = self.service
        class Provider(RecordingProvider):
            def chat(inner, messages, tools=None, temperature=0):
                if inner.cursor < len(inner.script) and "replay_original" in inner.script[inner.cursor]:
                    trace, = service.runs_dir.glob("*/trace.jsonl")
                    first = next(e for e in map(json.loads, trace.read_text().splitlines()) if
                                 e["kind"] == "tool_result" and e["tool"] == "run_browser" and not e["ok"])
                    inner.script[inner.cursor] = tool("rerun_browser_check", tool_call_id=first["tool_call_id"])
                return super().chat(messages, tools, temperature)
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=Provider(steps)):
            run = service.start(request("team", conversation=roster("lead", "builder", "qa"),
                objective="Build and independently verify a booking page showing A101 with 12 seats.",
                acceptance_files=[PRODUCT, "README.md"], workspace_path=str(self.project), limits={"max_steps":90}))
            detail = finished(service, run["id"])
        trace = self.service.runs_dir / run["id"] / "trace.jsonl"
        return detail, [json.loads(l) for l in trace.read_text().splitlines()]

    @staticmethod
    def author_steps(final_html):
        steps = [tool("delegate_task", assignee="builder", objective="Deliver the meeting room selector and instructions.",
                      deliverable_paths=[PRODUCT, "README.md"], remaining_roles=["qa"]),
                 write(SUITE, TEST_CODE), tool("run_shell", command=COMMAND+" 2>&1 | tail -10"),
                 write(PRODUCT, HTML), tool("run_browser", code=WRONG), tool("run_browser", code=CORRECTED),
                 {"replay_original": True}, write("README.md", README)]
        if final_html != HTML:
            steps.append(write(PRODUCT, final_html))
        return steps + [{"replay_original": True},
                        tool("ask_human", decision_kind="internal_routing", question="Which teammate should independently resolve this browser check?")]

    @staticmethod
    def decision(page, verdict, evidence):
        version = digest({k:file_hash(v) for k,v in {PRODUCT:page, SUITE:TEST_CODE, "README.md":README}.items()})
        criteria = ["independent_recheck", "probe_" + browser_scope(WRONG)]
        result = {"verdict":verdict, "evidence":[evidence],
                  "reason":"Independently measured the room selector and actual displayed seat count."}
        if verdict != "passed":
            result.update(cause="product", action="Restore the required 12-seat capacity.")
        return tool("submit_review", candidate_version=version, results=[{**result, "criterion_id":c} for c in criteria])

    def test_internal_scheduling_hands_both_obligations_to_the_independent_member(self):
        evidence = "pass:" + browser_scope(INDEPENDENT)
        steps = self.author_steps(HTML) + [
            tool("delegate_task", assignee="qa", objective="Recheck the frozen booking page."),
            tool("run_browser", code=INDEPENDENT), self.decision(HTML, "passed", evidence),
            tool("run_shell", command=COMMAND), self.decision(HTML, "passed", evidence),
            {"text":"The independent product check and the selected suite passed."}, {"text":"Delivered."}]
        detail, trace = self.run_room(steps)
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        self.assertEqual(detail["pending_questions"], [])
        self.assertFalse(any(e["kind"] == "human_question" for e in detail["room_events"]))
        queued, = [e for e in detail["room_events"] if e["kind"] == "author_probe_pending"]
        reports = [e["handoff"] for e in detail["room_events"] if e["kind"] == "member_report"]
        author, reviewer = reports
        self.assertEqual([r["role"] for r in reports], ["builder", "qa"])
        self.assertFalse(author["published"])
        self.assertEqual(queued["reviewer"], "qa")
        original = queued["original_calls"][browser_scope(WRONG)]
        original_failed, = [t for t in trace if t["kind"]=="tool_result" and t["tool_call_id"]==original]
        self.assertEqual((original_failed["task_id"], original_failed["tool"], original_failed["ok"]),
                         (author["task_id"],"run_browser",False))
        replayed = [t for t in trace if t["kind"]=="tool_result" and t["tool"]=="rerun_browser_check"]
        self.assertEqual([t["ok"] for t in replayed], [False,False])
        required, = queued["pending_verification"]
        self.assertEqual((required["scope"], required["path"], required["current_sha256"]),
                         (shell_invocation(COMMAND, ".").scope, SUITE, file_hash(TEST_CODE)))
        support, = queued["supporting_receipts"]
        self.assertEqual(support["original_calls"], queued["original_calls"])
        self.assertEqual(support["input_version"], digest({PRODUCT:file_hash(HTML), SUITE:file_hash(TEST_CODE)}))
        self.assertNotEqual(support["input_version"], queued["candidate_version"])
        checks = [t for t in trace if t["kind"]=="tool_result" and t["tool"]=="submit_review"]
        self.assertEqual([t["ok"] for t in checks], [False,True])
        self.assertIn(SUITE, checks[0]["preview"])
        self.assertEqual(reviewer["native_review"]["state"], "passed")
        self.assertEqual(reviewer["promoted_candidate"]["source_task"], author["task_id"])
        self.assertEqual((self.project / PRODUCT).read_text(), HTML)

    def test_regressed_room_capacity_cannot_publish_with_the_earlier_browser_pass(self):
        broken = HTML.replace("12 seats", "2 seats")
        steps = self.author_steps(broken) + [
            tool("delegate_task", assignee="qa", objective="Check the actual current capacity."),
            tool("run_shell", command=COMMAND), tool("run_browser", code=INDEPENDENT),
            self.decision(broken,"passed","pass:"+browser_scope(CORRECTED)),
            self.decision(broken,"changes_requested","fail:"+browser_scope(INDEPENDENT)),
            {"text":"[BLOCKED] The delivered selector shows two seats instead of twelve."},
            {"text":"[BLOCKED] The changed product still fails independent acceptance."},
            {"text":"[BLOCKED] The author did not restore the advertised capacity."},
            {"text":"[BLOCKED] A matching 12-seat product remains unavailable."}]
        detail, trace = self.run_room(steps)
        self.assertNotEqual(detail["run"]["status"], "completed")
        self.assertEqual(detail["pending_questions"], [])
        self.assertFalse(any(e["kind"]=="human_question" for e in detail["room_events"]))
        self.assertFalse((self.project / PRODUCT).exists())
        self.assertFalse(any(e["handoff"].get("promoted_candidate") for e in detail["room_events"] if e["kind"]=="member_report"))
        self.assertFalse(any(e["kind"]=="member_report" and e.get("handoff",{}).get("native_review",{}).get("state")=="passed"
                             for e in detail["room_events"]))
        pending, = [e for e in detail["room_events"] if e["kind"]=="author_probe_pending"]
        support, = pending["supporting_receipts"]
        self.assertNotEqual(support["input_version"], pending["candidate_version"])
        reviewer = next(e["handoff"] for e in detail["room_events"] if e["kind"]=="member_report" and e["role"]=="qa")
        decision = reviewer["native_review"]["assessment"]
        self.assertEqual((decision["candidate_version"], decision["verdict"]),
                         (pending["candidate_version"], "changes_requested"))
        self.assertTrue(all(f["cause"]=="product" for f in decision["results"]))
        checks = [e for e in trace if e["kind"]=="tool_result" and e.get("task_id")==reviewer["task_id"]]
        self.assertTrue(any(e["tool"]=="run_shell" and e["ok"] for e in checks))
        self.assertTrue(any(e["tool"]=="run_browser" and not e["ok"] for e in checks))
        self.assertEqual([e["ok"] for e in checks if e["tool"]=="submit_review"], [False,True])


if __name__=="__main__":
    unittest.main()
