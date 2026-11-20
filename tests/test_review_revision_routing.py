"""A signed request for product changes survives a failed review attachment handoff."""
import json
import unittest
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.crew_progress import _check_key, _script_inputs
from miniagent.studio_service import StudioService
from tests.support import cleanup, make_temp_dir
from tests.test_studio_conversations import RecordingProvider, tool
from tests.test_studio_crew import roster, write
from tests.test_studio_native_review import PATH, RULE, version
from tests.test_studio_service import finished, request


class ReviewRevisionRoutingTest(unittest.TestCase):
    def test_changed_candidate_after_signing_cannot_be_routed_as_a_current_repair(self):
        root = make_temp_dir("review-stale-rejection")
        self.addCleanup(cleanup, root)
        service = StudioService(root / "runs", Settings(api_key="test-credential"))
        self.addCleanup(service.close)
        old = "Original destination label."
        batch = tool("submit_review", candidate_version=version(old), results=[
            {"criterion_id":"content", "verdict":"changes_requested", "cause":"product",
             "reason":"The destination is missing.", "action":"Name the destination.", "evidence":["file:" + PATH]}])
        batch["tool_calls"] += tool("run_python", code="from pathlib import Path\nPath('delivery.txt').write_text('different bytes')")["tool_calls"]
        steps = [tool("delegate_task", assignee="author", objective="Publish the label.", deliverable_paths=[PATH]),
                 write(PATH, old), {"text":"Published."},
                 tool("delegate_task", assignee="risk", objective="Check the label.", review={"candidate":PATH,"criteria":[RULE]}),
                 tool("read_file", path=PATH), batch, {"text":"[BLOCKED] My review workspace has changed."},
                 {"text":"[BLOCKED] The signature no longer matches."}]
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=RecordingProvider(steps)):
            run = service.start(request("team", conversation=roster("lead","author","risk"), objective="Verify the label.",
                                        acceptance_files=[PATH], limits={"max_steps":60}))
            detail = finished(service, run["id"])
        self.assertNotEqual(detail["run"]["status"], "completed")
        entry, = [e for e in detail["room_events"] if e["kind"] == "member_report" and e["role"] == "risk"]
        self.assertIsNone(entry["handoff"]["native_review"]["assessment"])
        self.assertNotIn("review_action", entry["handoff"])
        self.assertEqual(entry["handoff"]["native_review"]["state"], "blocked")

    def test_signed_product_change_routes_without_publishing_reviewer_tree(self):
        old = "Shipment destination: Old Warehouse. Review before releasing."
        new = "Shipment destination: New Warehouse. Verified dispatch record."
        probe = ("from pathlib import Path\ntext = Path('delivery.txt').read_text()\n"
                 "assert 'New Warehouse' in text\nprint('PASS destination')\n")
        scope = _check_key(_script_inputs(probe)[1])
        execution = {"id":"run", "description":"The checked label uses the new warehouse.", "method":"execute"}
        rejection = [{"criterion_id":"content", "verdict":"changes_requested", "cause":"product",
                      "reason":"The old destination is printed.", "action":"Use New Warehouse on the label.",
                      "evidence":["file:" + PATH]},
                     {"criterion_id":"run", "verdict":"inconclusive", "cause":"verification",
                      "reason":"The first extraction needs another check.", "action":"Rerun against the actual label.",
                      "evidence":["fail:" + scope]}]
        accepted = [{"criterion_id":"content", "verdict":"passed", "reason":"Read the new destination.", "evidence":["file:" + PATH]},
                    {"criterion_id":"run", "verdict":"passed", "reason":"Executed the destination check.", "evidence":["pass:" + scope]}]
        for member_close in ("Signed the review and wrote the report.", "[BLOCKED] The product requires an author revision."):
            with self.subTest(member_close=member_close):
                root = make_temp_dir("review-revision")
                self.addCleanup(cleanup, root)
                local = root / "project"
                local.mkdir()
                service = StudioService(root / "runs", Settings(api_key="test-credential"))
                self.addCleanup(service.close)
                report = "risk_review.md"
                cache = "sources/blocked.json"
                steps = [tool("delegate_task", assignee="author", objective="Publish the label.", deliverable_paths=[PATH]),
                         write(PATH, old), {"text":"The original label is available."},
                         tool("delegate_task", assignee="risk", objective="Review the label.", deliverable_paths=[report],
                              review={"candidate":PATH, "criteria":[RULE,execution]}),
                         write(report, "Change the printed destination."), write(cache, json.dumps({"fixture":"sk-" + "x" * 40})),
                         tool("read_file", path=PATH), tool("run_python", code=probe),
                         tool("submit_review", candidate_version=version(old), results=rejection), {"text":member_close},
                         tool("delegate_task", assignee="author", objective="Apply the signed requirements."),
                         write(PATH, new), {"text":"Changed to New Warehouse."},
                         tool("delegate_task", assignee="risk", objective="Independently review the corrected label."),
                         tool("read_file", path=PATH), write(report, "Validated the new destination."),
                         tool("run_python", code=probe), tool("submit_review", candidate_version=version(old), results=accepted),
                         tool("submit_review", candidate_version=version(new), results=accepted[:1]),
                         tool("submit_review", candidate_version=version(new), results=accepted), {"text":"Delivered."}]
                provider = RecordingProvider(steps)
                with patch("miniagent.studio_service.DeepSeekProvider", return_value=provider):
                    run = service.start(request("team", conversation=roster("lead","author","risk"),
                        objective="Deliver a checked label and its risk report.", acceptance_files=[PATH,report],
                        workspace_path=str(local), limits={"max_steps":90}))
                    detail = finished(service, run["id"])
                events = [e for e in detail["room_events"] if e["kind"] == "member_report"]
                self.assertEqual(detail["run"]["status"], "completed", [(e["role"], e["handoff"]["remaining_issue"]) for e in events])
                self.assertEqual([e["role"] for e in events], ["author","risk","author","risk"])
                original, repaired, final = [e["handoff"] for e in events[1:]]
                self.assertFalse(original["published"])
                self.assertEqual(events[1]["disposition"], "assessed")
                self.assertIn("服务凭据" if not member_close.startswith("[BLOCKED]") else "[BLOCKED]", original["remaining_issue"])
                self.assertEqual(original["review_action"], {"state":"repair","target":events[0]["task_id"],
                    "candidate_version":version(old),"assignee":"author","reviewer":"risk","criteria":["content"]})
                self.assertEqual([r["native_review"]["state"] for r in (original,repaired,final)], ["repair","review","passed"])
                self.assertEqual(original["native_review"]["assessment"]["verdict"], "changes_requested")
                self.assertEqual(final["native_review"]["candidate_version"], version(new))
                self.assertEqual(final["deliverable_paths"], [report])
                self.assertEqual((local / report).read_text(), "Validated the new destination.")
                self.assertFalse((local / cache).exists())
                trace = [json.loads(line) for line in (root/"runs"/run["id"]/"trace.jsonl").read_text().splitlines()]
                self.assertTrue(all(e["ok"] for e in trace if e.get("kind") == "tool_result" and e.get("tool") == "delegate"))
                submits = [e["ok"] for e in trace if e.get("kind") == "tool_result" and e.get("tool") == "submit_review"]
                self.assertEqual(submits, [True,False,False,True])
