import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from miniagent.studio_crew import RoomCrew
from miniagent.studio_review_contract import register
from miniagent.studio_reviews import Review, Rule


class GroundedReviewContractTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        data = {"guide.md": "Published guide", "policy.md": "Round up to the next unit.", "other.md": "Other document"}
        manifest = {name: hashlib.sha256(content.encode()).hexdigest() for name, content in data.items()}
        for name, content in data.items():
            (self.root / name).write_text(content, encoding="utf-8")
        self.review = Review("t", "author", "qa", "Write guide", [Rule(
            id="independent_recheck", description="Check guide", method="inspect")], manifest,
            requirements="Check the guide against the rounding policy.", delivery_paths=("guide.md",))
        self.session = SimpleNamespace(item=self.review, reference_paths=set(), receipts={})
        self.ctx = SimpleNamespace(sandbox=SimpleNamespace(root=self.root))
        self.criterion = dict(id="rounding", description="Recalculate a non-integer example from the guide.",
                              user_quote="rounding policy", basis_path="policy.md", basis_quote="Round up",
                              artifact_paths=["guide.md"], method="execute")
        self.version = self.review.version

    def register(self, *criteria):
        return register(self.session, self.ctx, {"candidate_version":self.version, "criteria":list(criteria)})

    def read_policy(self):
        self.session.receipts["r1"] = {"kind":"read", "extent":26, "path":"policy.md",
                                       "sha256":self.review.files["policy.md"]}

    def test_evidence_sources_and_complete_scope_are_validated_atomically(self):
        self.assertFalse(self.register(self.criterion).ok)
        self.read_policy()
        for changes in ({"user_quote":"unrequested"}, {"basis_quote":"Round down"}, {"basis_path":"guide.md"},
                        {"artifact_paths":["missing.md"]}, {"artifact_paths":["other.md"]}, {"id":"probe_fake"}):
            with self.subTest(changes=changes):
                self.assertFalse(self.register(self.criterion | changes).ok)
                self.assertFalse(self.review.contract)
        (self.root / "policy.md").write_text("Round up after changing the policy")
        self.assertFalse(self.register(self.criterion).ok)

    def test_reviewer_can_choose_inspection_while_existing_execution_obligation_stays_bound(self):
        self.review.criteria = [Rule(id="independent_recheck", description="Rerun the original check.", method="execute")]
        self.read_policy()
        inspection = self.criterion | {"method": "inspect", "description": "Read the guide's rounding instructions."}
        result = self.register(inspection)
        self.assertTrue(result.ok, result.content)
        self.assertCountEqual([(rule.id, rule.method) for rule in self.review.criteria],
                              [("rounding", "inspect"), ("independent_recheck", "execute")])
        self.assertEqual(self.review.contract["definitions"], [inspection])

    def test_contract_survives_repairs_and_cannot_be_weakened_by_resubmission(self):
        self.read_policy()
        first = self.register(self.criterion)
        self.assertTrue(first.ok, first.content)
        self.assertEqual(self.review.contract["basis_versions"], {"policy.md":self.review.files["policy.md"]})
        self.assertEqual(self.register(self.criterion).meta["contract_id"], first.meta["contract_id"])
        self.assertFalse(self.register(self.criterion | {"description":"Open the file"}).ok)
        self.review.repaired(self.review.files | {"guide.md":"a" * 64}, ["guide.md"])
        self.assertNotEqual(self.review.version, self.version)
        self.assertFalse(self.register(self.criterion).ok)
        self.assertEqual(self.review.contract["registered_version"], self.version)
        self.assertEqual([r.id for r in self.review.criteria], ["rounding"])

    def test_signing_one_document_does_not_certify_an_entire_workspace_snapshot(self):
        self.read_policy()
        self.assertTrue(self.register(self.criterion).ok)
        self.review.state = "passed"
        self.review.latest = {"candidate_version":self.version}
        crew = SimpleNamespace(delivery={"files":["guide.md", "other.md"]},
                               native_reviews=SimpleNamespace(items={"t":self.review},
                                                             public_versions=self.review.files))
        self.assertEqual(RoomCrew._reviewed_delivery(crew), ())
        crew.delivery = {"files":["guide.md"]}
        receipt, = RoomCrew._reviewed_delivery(crew)
        self.assertEqual(receipt["files"], {"guide.md":self.review.files["guide.md"]})
