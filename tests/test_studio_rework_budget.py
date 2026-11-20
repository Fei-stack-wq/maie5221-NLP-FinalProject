import time
import unittest
from unittest.mock import patch

from miniagent.config import Settings
from miniagent.rao import Budget
from miniagent.studio_crew import RoomCrew
from miniagent.studio_service import StudioService
from tests.support import cleanup, make_temp_dir
from tests.test_studio_conversations import RecordingProvider, tool
from tests.test_studio_crew import assign, roster, write
from tests.test_studio_native_review import PATH, RULE, submit
from tests.test_studio_service import finished, request


class ReworkBudgetTest(unittest.TestCase):
    def test_bound_repair_and_second_signature_can_use_time_from_an_unstarted_release(self):
        root = make_temp_dir("rework-budget")
        self.addCleanup(cleanup, root)
        service = StudioService(root / "runs", Settings(api_key="test-credential"))
        self.addCleanup(service.close)
        old, new = "Ship to the old warehouse.", "Ship to the new warehouse."
        steps = [
            tool("delegate_task", assignee="dev", objective="Write the shipment label.",
                 deliverable_paths=[PATH], remaining_roles=["qa", "release"]),
            write(PATH, old), {"text": "First shipment label ready."},
            tool("delegate_task", assignee="qa", objective="Check the shipment label.",
                 review={"candidate": "dev", "criteria": [RULE]}),
            tool("read_file", path=PATH),
            submit(old, "changes_requested", cause="product", action="Use the new warehouse."),
            {"text": "The destination needs correction."},
            assign("dev", "Repair the label."), write(PATH, new), {"text": "Corrected."},
            assign("qa", "Recheck the new label."), tool("read_file", path=PATH),
            submit(new, "passed"), {"text": "The new shipment label passed."},
            assign("release", "Confirm availability for release."), tool("read_file", path=PATH),
            {"text": "The reviewed artifact is available."}, {"text": "Delivered."},
        ]
        dispatch = RoomCrew._dispatch

        def simulate_elapsed_time(room, role, *args, **kwargs):
            result = dispatch(room, role, *args, **kwargs)
            if role == "qa" and room._next_review() and room._next_review().state == "repair":
                # Original room budget was one hour, with 693 s forecast for release.
                # At the second rejected real attempt only ~425 s remained.
                room.deadline = time.monotonic() + 425
            return result

        with patch.object(RoomCrew, "_dispatch", simulate_elapsed_time), patch(
                "miniagent.studio_service.DeepSeekProvider", return_value=RecordingProvider(steps)):
            run = service.start(request("team", conversation=roster("lead", "dev", "qa", "release", "unused"),
                                        objective="Write, independently verify, and release the shipment label.",
                                        acceptance_files=[PATH],
                                        limits={"max_seconds": 3600, "max_steps": 0, "max_total_tokens": 0}))
            detail = finished(service, run["id"])

        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        assignments = [e for e in detail["room_events"] if e["kind"] == "assignment"]
        self.assertEqual([e["recipient"] for e in assignments], ["dev", "qa", "dev", "qa", "release"])
        initial, _, repair, review, _ = assignments
        self.assertGreater(initial["protected_peer_shares"]["release"]["seconds_left"], 425)
        self.assertEqual(set(repair["protected_peer_shares"]), {"qa"})
        self.assertGreater(repair["protected_peer_shares"]["qa"]["seconds_left"], 0)
        self.assertGreater(repair["allocation"]["seconds_left"], 100)
        self.assertGreater(review["allocation"]["seconds_left"], 100)
        for event in [repair, review]:
            peer_seconds = sum(s["seconds_left"] for s in event["protected_peer_shares"].values())
            self.assertGreaterEqual(event["reserved"]["seconds_left"], 90)
            self.assertLessEqual(event["allocation"]["seconds_left"] + event["reserved"]["seconds_left"]
                                 + peer_seconds, 425)
        receipts = [e["handoff"]["native_review"] for e in detail["room_events"]
                    if e["kind"] == "member_report" and e["handoff"].get("native_review")]
        self.assertEqual([r["state"] for r in receipts], ["repair", "review", "passed"])
        self.assertNotEqual(receipts[0]["candidate_version"], receipts[-1]["candidate_version"])

    def test_finite_user_budget_can_fund_more_than_sixteen_completed_assignments(self):
        root = make_temp_dir("many-assignments")
        self.addCleanup(cleanup, root)
        service = StudioService(root / "runs", Settings(api_key="test-credential"))
        self.addCleanup(service.close)
        steps = []
        for i in range(17):
            path = f"part-{i}.txt"
            steps.extend([tool("delegate_task", assignee="writer", objective=f"Deliver part {i}",
                               deliverable_paths=[path], remaining_roles=["qa"]),
                          write(path, f"Completed shipment documentation for part {i}."), {"text": f"Delivered part {i}."}])
        steps.extend([assign("qa", "Confirm the final part."), tool("read_file", path="part-16.txt"),
                      {"text": "Read the final part."}, {"text": "All seventeen parts are ready."}])
        with patch("miniagent.studio_service.DeepSeekProvider", return_value=RecordingProvider(steps)):
            run = service.start(request("team", conversation=roster("lead", "writer", "qa"),
                                        objective="Produce seventeen documents in stages, then inspect the final part.",
                                        acceptance_files=["part-16.txt"], limits={"max_steps": 150}))
            detail = finished(service, run["id"])
        self.assertEqual(detail["run"]["status"], "completed", detail["diagnostics"])
        self.assertEqual([e["role"] for e in detail["room_events"] if e["kind"] == "member_report"],
                         ["writer"] * 17 + ["qa"])

    def test_mandatory_participants_and_final_answer_still_reserve_finite_budget(self):
        room = object.__new__(RoomCrew)
        room.direct = False
        room.thread, room.child_checks = {"addressed_roles": ["named"]}, {"tested": {}}
        room.approved_roles, room.covered_roles, room.started_roles = set(), set(), set()
        room.untapped_shares = {}
        room.close_tokens, room.close_seconds = 12000, 90
        room.deadline = time.monotonic() + 3600
        room.budget = Budget(steps_left=120, actions_left=400, tokens_left=1000000, seconds_left=3600)
        plan = {"dev", "qa", "release"}
        room._allocation("dev", plan)

        # No roster fan-out for an unplanned member. User-assigned checks and
        # participants still receive real capacity when the optional plan won't fit.
        room.budget = Budget(steps_left=9, actions_left=6, tokens_left=60000, seconds_left=425)
        child, close = room._allocation("dev", plan)
        self.assertFalse(child.exhausted)
        self.assertEqual(set(room.untapped_shares), {"tested", "named"})
        for field in Budget.model_fields:
            total = sum(getattr(share, field) for share in (child, close, *room.untapped_shares.values()))
            self.assertLessEqual(total, getattr(room.budget, field))
        room.budget.tokens_left = 12000
        child, _ = room._allocation("dev", None)
        self.assertFalse(child.has_tokens(1000))
        room.budget = Budget(steps_left=None, actions_left=None, tokens_left=None, seconds_left=90)
        child, _ = room._allocation("dev", None)
        self.assertTrue(child.exhausted)
