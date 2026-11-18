import unittest

from miniagent.studio_crew import RoomCrew


class RevisedMemberChecksTest(unittest.TestCase):
    def test_peer_must_see_same_revision_run_checks_and_attest(self):
        crew = RoomCrew.__new__(RoomCrew)
        crew.pending_check_reviews = {}
        revision = {"path": "pricing-test.py", "prior_sha256": "aaa", "current_sha256": "bbb",
                    "current_caption": "quarterly total", "prior_failure": "unexpected total"}
        crew._record_check_reviews("builder", "task-original", "changed my expectation",
                                   {"superseded_member_checks": [revision]}, {}, [])
        note = "[CHECKED task-original] compared against user formula"
        checked = {"check_signals": ["2 assertions passed"]}
        src = {"pricing-test.py": "bbb"}
        for role, text, evidence, snapshot, changes in [
                ("builder", note, checked, src, []),
                ("tester", "looks right", checked, src, []),
                ("tester", note, {}, src, []),
                ("tester", note, checked, {"pricing-test.py": "old"}, []),
                ("tester", note, checked, src, [{"path": "pricing-test.py"}])]:
            with self.subTest(role=role, text=text, changes=changes):
                self.assertFalse(crew._record_check_reviews(role, "task-review", text, evidence, snapshot, changes))
                self.assertIn("task-original", crew.pending_check_reviews)
        self.assertEqual(crew._record_check_reviews("tester", "task-review", note, checked, src, []), ["task-original"])
        self.assertFalse(crew.pending_check_reviews)

    def test_uncertain_checked_candidate_needs_same_scope_on_peer_snapshot(self):
        crew = object.__new__(RoomCrew)
        crew.pending_check_reviews = {}
        subject = {"scope": "scope-a", "check": "python3 test_inventory.py",
                   "path": "test_inventory.py", "current_sha256": "v2"}
        crew._record_check_reviews("builder", "candidate", "", {"pending_verification": [subject]}, {}, [])
        attestation = "[CHECKED candidate] verified"
        verified = {"check_signals": ["suite passed"], "complete_verification": {"scope-a": "test_inventory.py"}}
        original = {"test_inventory.py": "v2"}
        for role, evidence, files, changes in [
            ("builder", verified, original, []),
            ("qa", {"check_signals": ["another suite passed"]}, original, []),
            ("qa", verified, {"test_inventory.py": "v1"}, []),
            ("qa", verified, original, [{"path":"test_inventory.py", "sha256":"v3"}]),
        ]:
            self.assertFalse(crew._record_check_reviews(role, "other", attestation, evidence, files, changes))
        self.assertIn("candidate", crew.pending_check_reviews)
        self.assertFalse(crew._record_check_reviews("qa", "other", attestation, verified, {"test_inventory.py":"v3"}, []))
        self.assertEqual(crew._record_check_reviews("release", "other", attestation, verified, {"test_inventory.py":"v3"}, []), ["candidate"])
