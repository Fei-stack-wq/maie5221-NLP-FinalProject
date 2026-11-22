"""Criterion-specific receipts from real independent tools on frozen deliverables."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from miniagent.config import Limits
from miniagent.crew_progress import MemberProgress
from miniagent.rao import Observation, RAOState, StepRecord
from miniagent.sandbox import Sandbox
from miniagent.studio_reviews import Review, ReviewSession, Rule
from miniagent.tools import ToolContext, build_default_registry


class ReviewArtifactEvidenceTest(unittest.TestCase):
    def start(self, rules, files):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for path, data in files.items():
            (root / path).write_text(data)
        manifest = {name: hashlib.sha256(data.encode()).hexdigest() for name, data in files.items()}
        self.progress = MemberProgress(root)
        self.ctx = ToolContext(Sandbox(root), Limits())
        self.registry = build_default_registry()
        review = Review("release", "writer", "qa", "Independent release check", rules, manifest)
        self.session = ReviewSession(review, manifest, self.progress, "attempt",
                                     available_tools=self.registry.names())
        self.session.guard_file_tools(self.registry)
        self.step = 0

    def call(self, name, **args):
        self.step += 1
        call_id = str(self.step)
        result = self.registry.dispatch(name, args, self.ctx)
        action = {"role": "assistant", "tool_calls": [{"id": call_id, "function": {
            "name": name, "arguments": json.dumps(args)}}]}
        obs = Observation(step_id=call_id, source=name, ok=result.ok, content=result.content,
                          meta=result.meta, tool_call_id=call_id)
        record = StepRecord(step=self.step, observations=[obs], prompt_tokens=500)
        self.progress.observe(record, RAOState(goal="", task_id="review", history=[action]))
        self.session.observe(record)
        return [r for r in self.session.receipts.values() if r["tool_call_id"] == call_id]

    @staticmethod
    def rule(id, method, paths):
        return Rule(id=id, description=id, method=method, artifact_paths=paths)

    def submit(self, **findings):
        return self.session.submit(None, {"candidate_version": self.session.item.version, "results": [
            {"criterion_id": id, "verdict": "passed", "reason": "Independent check", "evidence": refs}
            for id, refs in findings.items()]})

    def test_shared_run_only_supports_the_named_assertions_that_read_each_frozen_file(self):
        self.start([self.rule("cash", "execute", ["proposal.txt"]), self.rule("agree", "execute", ["proposal.txt", "intro.txt"])],
                   {"proposal.txt": "4 10", "intro.txt": "4 10"})
        setup = ("from pathlib import Path\na=tuple(map(int,Path('proposal.txt').read_text().split()))\n"
                 "b=tuple(map(int,Path('intro.txt').read_text().split()))\n")
        weak, = self.call("run_python", code=setup + "assert a == (4, 10), 'cash'\nassert True, 'agree'\nprint('PASS cases')")
        failed = self.submit(cash=[weak["ref"]], agree=[weak["ref"]])
        self.assertFalse(failed.ok)
        self.assertEqual([i["criterion_id"] for i in failed.meta["issues"]], ["agree"])
        separate, = self.call("run_python", code=setup + "assert a, 'agree'\nassert b, 'agree'\nprint('PASS parts')")
        self.assertFalse(self.submit(cash=[weak["ref"]], agree=[separate["ref"]]).ok)
        shortcut, = self.call("run_python", code=setup + "assert True or a == b, 'agree'\nprint('PASS shortcut')")
        self.assertFalse(self.submit(cash=[weak["ref"]], agree=[shortcut["ref"]]).ok)
        valid, = self.call("run_python", code=setup + "assert a == (7-3,7+3), 'cash'\nassert a == b, 'agree'\nprint('PASS cases')")
        self.assertTrue(self.submit(cash=[valid["ref"]], agree=[valid["ref"]]).ok)
        self.assertEqual(self.session.catalog()["evidence"][-1]["criterion_ids"], ["agree", "cash"])

    def test_one_annotated_failure_rejects_the_incorrect_frozen_bounds(self):
        self.start([self.rule("cash", "execute", ["proposal.txt"])], {"proposal.txt": "5 9"})
        code = ("from pathlib import Path\nbounds=tuple(map(int,Path('proposal.txt').read_text().split()))\n"
                "assert bounds == (7-3,7+3), 'cash'\nprint('PASS cash')")
        check, = [r for r in self.call("run_python", code=code) if r["kind"] == "failed_check"]
        self.assertIn("cash", check.get("criterion_inputs", {}))
        result = self.session.submit(None, {"candidate_version": self.session.item.version, "results": [
            {"criterion_id": "cash", "verdict": "changes_requested", "cause": "product",
             "action": "Correct the cash range.", "reason": "Expected 4 and 10, observed 5 and 9.", "evidence": [check["ref"]]}]})
        self.assertTrue(result.ok, result.content)
        self.assertEqual(self.session.assessment["verdict"], "changes_requested")

    def test_native_browser_checks_bind_each_named_assertion_to_its_actual_document(self):
        self.start([self.rule("page_a", "execute", ["a.html"]), self.rule("page_b", "execute", ["b.html"])],
                   {"a.html": "<h1>Alpha</h1>", "b.html": "<h1>Beta</h1>"})
        code = ("from pathlib import Path\npage.goto(Path('a.html').resolve().as_uri())\n"
                "assert page.locator('h1').inner_text() == 'Alpha', 'page_a'\n"
                "page.goto(Path('b.html').resolve().as_uri())\n"
                "expect(page.locator('h1'), 'page_b').to_have_text('Beta')\n"
                "assert True, 'unobserved'")
        check, = self.call("run_browser", code=code)
        proofs = check["criterion_inputs"]
        self.assertEqual({key: [list(paths) for paths in checks] for key, checks in proofs.items()},
                         {"page_a": [["a.html"]], "page_b": [["b.html"]]})
        self.assertTrue(self.submit(page_a=[check["ref"]], page_b=[check["ref"]]).ok)

    def test_clean_early_exit_does_not_attest_a_later_assertion(self):
        self.start([self.rule("cash", "execute", ["proposal.txt"])], {"proposal.txt": "5 9"})
        rows = self.call("run_python", code=("from pathlib import Path\nprint('PASS cash')\nraise SystemExit(0)\n"
                        "assert Path('proposal.txt').read_text() == '4 10', 'cash'"))
        self.assertFalse(any(row.get("criterion_inputs") for row in rows))
        self.assertIn("cash", self.session.catalog()["needs"])

    def test_inspect_binds_both_files_and_allows_the_same_read_to_support_separate_findings(self):
        self.start([self.rule("consistent", "inspect", ["a.txt", "b.txt"]), self.rule("format", "inspect", ["a.txt"])],
                   {"a.txt": "Original content", "b.txt": "Matching explanation"})
        a, = self.call("read_file", path="a.txt")
        partial = self.submit(consistent=[a["ref"]], format=[a["ref"]])
        self.assertFalse(partial.ok)
        self.assertIn("b.txt", partial.content)
        b, = self.call("read_file", path="b.txt")
        self.assertTrue(self.submit(consistent=[a["ref"], b["ref"]], format=[a["ref"]]).ok)
