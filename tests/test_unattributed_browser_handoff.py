"""A member can hand a frozen product to QA even if its original test never reached the page."""
import json
import unittest
from collections import Counter
from unittest.mock import patch

from miniagent.agent import COORDINATOR_PROMPT
from miniagent.config import Settings
from miniagent.studio_service import StudioService
from tests.support import cleanup, make_temp_dir
from tests.test_bound_reviewer_browser import browser_scope
from tests.test_mixed_browser_script_handoff import CORRECTED, HTML, INDEPENDENT, PRODUCT
from tests.test_studio_conversations import RecordingProvider, tool
from tests.test_studio_crew import roster, write
from tests.test_studio_service import finished, request


# Reproduces the pre-navigation failure of the meeting-room run: the runner
# already owns Playwright, and the nested session fails before any file is opened.
STARTUP_FAILURE = (
    "from playwright.sync_api import sync_playwright\n"
    "with sync_playwright() as nested:\n"
    "    nested.chromium.name\n"
    "assert page.get_by_label('Meeting room').count() == 1\n"
)
UNRELATED = (
    "from pathlib import Path\n"
    "page.goto(Path('index.html').resolve().as_uri())\n"
    "assert page.title() == 'Earlier project'"
)
BLOCKED = "[BLOCKED] 草稿已写完，修订后的浏览器检查已通过。请 QA 独立复核冻结草稿和原浏览器检查。"


class SearchingLead(RecordingProvider):
    """Only one model-initiated delegation exists, regardless of the lead's prompts."""
    def __init__(self, runs):
        super().__init__([])
        self.runs = runs
        self.turns = Counter()
        self.lead_requests_when_qa_started = None
        self.author_actions = iter([
            write(PRODUCT, HTML),
            tool("run_browser", code=STARTUP_FAILURE),
            tool("run_browser", code=CORRECTED),
            {"text": BLOCKED},
        ])

    def sign(self, code):
        room, = self.runs.glob("*/public-room.jsonl")
        author = next(event["handoff"] for event in map(json.loads, room.read_text().splitlines())
                      if event["kind"] == "member_report" and event["role"] == "dev")
        result = {"verdict": "passed", "evidence": ["pass:" + browser_scope(code)],
                  "reason": "The independent browser exercise checked the room and capacity on the frozen page."}
        criteria = ["independent_recheck", "probe_" + browser_scope(STARTUP_FAILURE)]
        return tool("submit_review", candidate_version=author["reviewable_candidate"]["candidate_version"],
                    results=[{**result, "criterion_id": criterion} for criterion in criteria])

    def chat(self, messages, tools=None, temperature=0):
        system = messages[0]["content"]
        if system.startswith(COORDINATOR_PROMPT):
            self.turns["lead"] += 1
            if self.turns["lead"] == 1:
                reply = tool("delegate_task", assignee="dev", objective="Deliver a meeting room selector.",
                             deliverable_paths=[PRODUCT], remaining_roles=["qa"])
            elif not tools:
                reply = {"text": "Reporting the signed independent result."}
            elif self.turns["lead"] <= 4:
                reply = tool("find_files", pattern=PRODUCT)
            else:
                reply = {"error": "The frozen handoff did not advance; the lead only searches."}
        elif "「dev」" in system:
            reply = next(self.author_actions, {"text": BLOCKED})
        elif "「qa」" in system:
            self.turns["qa"] += 1
            if self.turns["qa"] == 1:
                self.lead_requests_when_qa_started = self.turns["lead"]
            if self.turns["qa"] in (1, 3):
                reply = tool("run_browser", code=UNRELATED if self.turns["qa"] == 1 else INDEPENDENT)
            elif self.turns["qa"] in (2, 4):
                reply = self.sign(UNRELATED if self.turns["qa"] == 2 else INDEPENDENT)
            else:
                reply = {"text": "Independently checked the original frozen product."}
        else:
            raise AssertionError(f"Unexpected system identity: {system[:40]}")
        self.script.append(reply)
        return super().chat(messages, tools, temperature)


class UnattributedBrowserHandoffTest(unittest.TestCase):
    def test_private_draft_gets_independent_qa_even_if_lead_only_searches(self):
        root = make_temp_dir("unattributed-browser-handoff")
        self.addCleanup(cleanup, root)
        project = root / "workspace"
        project.mkdir()
        # Other public HTML files were present in the real run, so selecting
        # the one HTML file in the workspace would resolve the wrong question.
        (project / "index.html").write_text("<title>Earlier project</title>")
        service = StudioService(root / "runs", Settings(api_key="test-only"))
        self.addCleanup(service.close)
        provider = SearchingLead(service.runs_dir)
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider):
            task = service.start(request("team", conversation=roster("lead", "dev", "qa"),
                objective="Build and independently verify the meeting-room selector showing A101 with 12 seats.",
                acceptance_files=[PRODUCT], workspace_path=str(project), interaction_mode="autonomous"))
            detail = finished(service, task["id"])
        events = detail["room_events"]
        reports = [e["handoff"] for e in events if e["kind"] == "member_report"]
        author = reports[0]
        self.assertFalse(author["published"])
        failure, = author["evidence"]["failed_check_details"]
        self.assertIn("Playwright Sync API", failure["error_excerpt"])
        self.assertTrue(author["evidence"]["check_signals"], "Author's corrected self-check passed.")
        self.assertTrue(author["evidence"]["observed_failure"], "Self-check cannot sign off the old failure.")
        self.assertEqual([e["recipient"] for e in events if e["kind"] == "assignment"], ["dev", "qa"])
        self.assertEqual(provider.lead_requests_when_qa_started, 1, "QA must start directly after dev hands off.")
        self.assertFalse(detail["pending_questions"])
        _, reviewer = reports
        native = reviewer["native_review"]
        self.assertEqual(native["candidate_version"], author["reviewable_candidate"]["candidate_version"])
        self.assertEqual(native["assessment"]["evaluator"], "qa")
        self.assertEqual(native["assessment"]["verdict"], "passed")
        self.assertEqual(reviewer["promoted_candidate"]["source_task"], author["task_id"])
        final_lead = [messages for messages, tools in provider.requests
                      if not tools and messages[0]["content"].startswith(COORDINATOR_PROMPT)][-1]
        receipt = "\n".join(m.get("content") or "" for m in final_lead)
        self.assertIn('"promoted_candidate"', receipt)
        self.assertIn(author["task_id"], receipt)
        self.assertIn(reviewer["task_id"], receipt)
        trace = [json.loads(l) for l in (service.runs_dir / task["id"] / "trace.jsonl").read_text().splitlines()]
        qa = [e for e in trace if e.get("kind") == "tool_result" and e["task_id"] == reviewer["task_id"]]
        self.assertEqual([(e["tool"], e["ok"]) for e in qa if e["tool"] in {"submit_review", "run_browser"}],
                         [("run_browser", True), ("submit_review", False), ("run_browser", True), ("submit_review", True)])
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        self.assertEqual((project / PRODUCT).read_text(), HTML)


if __name__ == "__main__":
    unittest.main()
