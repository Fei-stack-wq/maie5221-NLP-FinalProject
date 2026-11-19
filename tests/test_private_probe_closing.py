"""A source adjudicator can finish the exact pending check inherited privately."""
import hashlib
import json
import unittest
from copy import deepcopy
from unittest.mock import patch

from miniagent.check_provenance import shell_invocation
from miniagent.config import Settings
from miniagent.crew_progress import _check_key, _script_layout, _shell_case_suite
from miniagent.research import Page
from miniagent.studio_reviews import digest
from miniagent.studio_service import StudioService
from tests.support import cleanup, make_temp_dir
from tests.test_bound_reviewer_probe import LABEL, PATH, SOURCE_RULE, SOURCE_URL, ProbeProvider
from tests.test_studio_conversations import tool
from tests.test_studio_crew import roster, write
from tests.test_studio_service import finished, request


SCRIPT = "checks/source_check.py"
COMMAND = "python3 " + SCRIPT
CAPTION = "source confirms the quantity"
BAD = (
    "from pathlib import Path\n"
    "text = Path('delivery.txt').read_text()\n"
    "confirmed = '20 units' in text\n"
    f"print(('ok ' if confirmed else 'FAIL ') + {CAPTION!r})\n"
    "assert confirmed, 'quantity extraction is incorrect'\n"
    f"print('PASS ' + {CAPTION!r})\n"
)
FIXED = BAD.replace("20 units", "10 units")
FAILED_SCOPE = _check_key(_shell_case_suite(COMMAND, ".") + "\0sha256=" +
                          hashlib.sha256(BAD.encode()).hexdigest(), CAPTION)
PENDING_SCOPE = shell_invocation(COMMAND, ".").scope


class PrivateClosingProvider(ProbeProvider):
    def __init__(self, script, root):
        super().__init__(script)
        self.root = root
        self.windows = []

    def chat(self, messages, tools=None, temperature=0):
        if self.cursor < len(self.script) and "_closing_command" in self.script[self.cursor]:
            row = self.script[self.cursor]
            specs = {t.name: t for t in tools or []}
            self.windows.append({"index": len(self.requests), "tools": sorted(specs),
                                 "schema": deepcopy(specs["run_shell"].parameters) if "run_shell" in specs else None})
            if row.get("_change_sha"):
                candidates = list(self.root.glob("runs/*/assignments/*/agents/arbiter/" + SCRIPT))
                if len(candidates) != 1:
                    raise AssertionError(f"Expected one actual arbiter workspace, found {candidates}")
                # A deterministic external edit between offering and executing
                # the capability must invalidate it; this is not a model tool.
                candidates[0].write_text(FIXED + "# changed after capability offer\n")
            self.script[self.cursor] = tool("run_shell", command=row["_closing_command"])
        return super().chat(messages, tools, temperature)


class PrivateProbeClosingTest(unittest.TestCase):
    def setUp(self):
        self.root = make_temp_dir("private-probe-closing")
        self.service = StudioService(self.root / "runs", Settings(api_key="test-only"))
        self.addCleanup(cleanup, self.root)
        self.addCleanup(self.service.close)

    def execute(self, *, command=COMMAND, change_sha=False, reach_handoff=True):
        self.assertIsNone(_script_layout(BAD))
        self.assertIsNone(_script_layout(FIXED))
        version = digest({PATH: hashlib.sha256(LABEL.encode()).hexdigest()})
        finding = {"criterion_id": "probe_" + FAILED_SCOPE, "verdict": "passed",
                   "reason": "The old checker searched for twenty units; both original product and fresh source say ten.",
                   "evidence": ["file:" + PATH, "source:" + SOURCE_URL]}
        adjudicate = {"_probe_finding": finding, "_source_original": True}
        steps = [
            tool("delegate_task", assignee="builder", objective="Publish the shipment label.",
                 deliverable_paths=[PATH], remaining_roles=["risk"]),
            write(PATH, LABEL), {"text": "The label is published."},
            tool("delegate_task", assignee="risk", objective="Verify the label against the issuer source.",
                 deliverable_paths=["inspection.md"], remaining_roles=[],
                 review={"candidate": "builder", "criteria": [SOURCE_RULE]}),
            write("inspection.md", "Compare the label with the original issuer source."),
            write(SCRIPT, BAD),
            tool("run_shell", command=COMMAND + ' > checks/source_check.log 2>&1; echo "EXIT=$?"; tail -3 checks/source_check.log'),
            tool("run_shell", command=COMMAND), write(SCRIPT, FIXED), tool("run_shell", command=COMMAND),
            tool("append_file", path="inspection.md", content="\nThe extraction was wrong; the original failed scope needs a peer."),
            tool("http_get", url=SOURCE_URL), tool("read_file", path=PATH),
            tool("submit_review", candidate_version=version, results=[{
                "criterion_id": SOURCE_RULE["id"], "verdict": "passed",
                "reason": "The issuer confirms the label. The cited failed script used the wrong quantity literal.",
                "evidence": ["file:" + PATH, "source:" + SOURCE_URL, "fail:" + FAILED_SCOPE],
            }]),
            {"text": "[BLOCKED] An independent peer must adjudicate the original failed checker."},
            tool("delegate_task", assignee="arbiter", objective="Independently adjudicate the exact source-check discrepancy."),
            tool("http_get", url=SOURCE_URL), tool("read_file", path=PATH),
        ]
        if reach_handoff:
            # Repeated inspection reaches the runtime's ordinary closing phase;
            # no fake policy or budget mutation is used to expose the old bug.
            steps += [tool("read_file", path=PATH) for _ in range(3)]
        steps += [deepcopy(adjudicate), {"_closing_command": command, "_change_sha": change_sha},
                  deepcopy(adjudicate),
                  {"text": "Completed the source adjudication and the exact inherited pending check."},
                  {"text": "Delivered the independently source-verified label."}]
        provider = PrivateClosingProvider(steps, self.root)
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider), patch(
                "miniagent.research.fetch", return_value=Page(SOURCE_URL, "text/plain", LABEL.encode(), False)):
            run = self.service.start(request(
                "team", conversation=roster("lead", "builder", "risk", "arbiter"),
                objective="Publish a shipment label and verify the quantity against its issuer source.",
                acceptance_files=[PATH], required_source_review_paths=[PATH], limits={"max_steps": 100}))
            detail = finished(self.service, run["id"])
        reports = [event["handoff"] for event in detail["room_events"] if event["kind"] == "member_report"]
        trace = [json.loads(line) for line in (self.root / "runs" / run["id"] / "trace.jsonl").read_text().splitlines()]
        return detail, reports, provider, trace

    def assert_private_window(self, reports, provider):
        risk = next(r for r in reports if r["role"] == "risk")
        self.assertFalse(risk["published"])
        self.assertTrue(risk["evidence"]["pending_verification"])
        self.assertNotIn(PENDING_SCOPE, risk["evidence"]["complete_verification"])
        self.assertIn(FAILED_SCOPE, str(risk["evidence"]["failed_check_details"]))
        window, = provider.windows
        self.assertEqual(window["tools"], ["run_shell"])
        self.assertEqual(window["schema"]["properties"]["command"]["enum"], [COMMAND])
        self.assertEqual(window["schema"]["properties"]["cwd"]["enum"], ["."])
        self.assertFalse(window["schema"]["additionalProperties"])
        messages = str(provider.requests[window["index"]][0])
        self.assertIn("私有候选的指定检查尚未在当前版本独立完成", messages)
        self.assertNotIn("run_shell", provider.requests[window["index"] + 1][1],
                         "After this single offer, only bounded review submission may resume.")
        return next(r for r in reports if r.get("native_review", {}).get("kind") == "reviewer_probe")

    def test_private_source_probe_can_finish_exact_check_then_resubmit_in_closing(self):
        detail, reports, provider, trace = self.execute()
        self.assertEqual(detail["run"]["status"], "completed", [(r["role"], r["remaining_issue"]) for r in reports])
        child = self.assert_private_window(reports, provider)
        self.assertEqual(child["native_review"]["assessment"]["verdict"], "passed")
        self.assertIn(PENDING_SCOPE, child["evidence"]["complete_verification"])
        self.assertTrue(child["evidence"]["pending_verification"], "The old pending identity remains in diagnostic evidence.")
        submits = [r for r in trace if r["kind"] == "tool_result" and r["task_id"] == child["task_id"]
                   and r["tool"] == "submit_review"]
        self.assertEqual([r["ok"] for r in submits], [False, True])

    def test_complete_source_verdict_exposes_exact_obligation_during_normal_work_too(self):
        detail, reports, provider, _ = self.execute(reach_handoff=False)
        self.assertEqual(detail["run"]["status"], "completed", [(r["role"], r["remaining_issue"]) for r in reports])
        self.assert_private_window(reports, provider)

    def test_private_closing_capability_rejects_unrelated_shell(self):
        detail, reports, provider, _ = self.execute(command="touch unexpected.md")
        child = self.assert_private_window(reports, provider)
        self.assertNotEqual(detail["run"]["status"], "completed")
        self.assertIsNone(child["native_review"]["assessment"])
        self.assertNotIn(PENDING_SCOPE, child["evidence"]["complete_verification"])
        self.assertFalse(any(self.root.rglob("unexpected.md")))

    def test_private_closing_capability_rejects_script_changed_after_offer(self):
        detail, reports, provider, trace = self.execute(change_sha=True)
        child = self.assert_private_window(reports, provider)
        self.assertNotEqual(detail["run"]["status"], "completed")
        self.assertIsNone(child["native_review"]["assessment"])
        calls = [r for r in trace if r["kind"] == "tool_result" and r["task_id"] == child["task_id"]
                 and r["tool"] == "run_shell"]
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]["ok"])
        self.assertNotIn("exit_code", calls[0], "The altered script must not execute.")


if __name__ == "__main__":
    unittest.main()
