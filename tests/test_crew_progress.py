"""Crew scheduling receipts control reinspection and never certify acceptance."""
import hashlib
import json
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from miniagent.config import Limits
from miniagent.crew_progress import MemberProgress
from miniagent.rao import Budget, Observation, RAOState, StepRecord
from miniagent.sandbox import Sandbox
from miniagent.schemas import ToolResult
from miniagent.tools import ToolContext, build_default_registry


class MemberProgressTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.src = self.root / "module.py"
        self.src.write_text("# initial")
        self.p = MemberProgress(self.root)
        self.budget = Budget(steps_left=30, actions_left=100, tokens_left=90_000, seconds_left=120)

    def observe(self, tool="run_python", text="", *, args=None, ok=True, meta=None, call_id="one", step=1):
        message = {"role": "assistant", "tool_calls": [{"id": call_id, "function": {
            "name": tool, "arguments": json.dumps(args or {})}}]}
        state = RAOState(goal="", task_id="t", history=[message])
        obs = Observation(step_id="s", source=tool, ok=ok, content=text, meta=meta or {}, tool_call_id=call_id)
        self.p.observe(StepRecord(step=step, observations=[obs], prompt_tokens=500), state)

    def test_restored_failure_keeps_original_call_without_becoming_a_local_receipt(self):
        self.observe(args={"code": "assert 1 == 2"}, text="exit_code=1\nstderr:\nAssertionError",
                     ok=False, meta={"exit_code": 1}, call_id="original")
        failed = self.p.failed_checks()
        details = self.p.evidence()["failed_check_details"]
        successor = MemberProgress(self.root)
        successor.restore_failed_checks(failed, details)
        self.assertEqual(successor.evidence()["failed_check_details"], details)
        self.assertEqual(successor.failed_checks(), failed)
        self.assertFalse(successor.review_evidence())
        self.assertFalse(successor.inline_failures())

    def test_unrelated_python_assertion_does_not_explain_original_failure(self):
        read = "from pathlib import Path\nvalue=Path('module.py').read_text()\n"
        wrong = read + "assert 'missing' in value"
        right = read + "assert 'initial' in value\nprint('PASS actual value')"
        self.observe(args={"code":wrong}, ok=False, text="exit_code=1\nstderr:\nAssertionError", meta={"exit_code":1},
                     call_id="original", step=16)
        self.observe(args={"code":"assert 4 == 4\nprint('PASS unrelated')"}, text="exit_code=0\nstdout:\nPASS unrelated",
                     meta={"exit_code":0}, call_id="constant", step=17)
        self.assertFalse(self.p.corrected_probe_support())
        self.observe(args={"code":right}, text="exit_code=0\nstdout:\nPASS actual value", meta={"exit_code":0},
                     call_id="counterexample", step=18)
        original, = self.p.failed_checks()
        support, = self.p.corrected_probe_support()
        self.assertEqual(support["original_calls"], {original:"original"})
        self.assertEqual(support["tool_call_id"], "counterexample")
        self.src.write_text("regressed")
        self.observe("write_file", args={"path":"module.py"}, call_id="edit", step=19)
        self.observe(args={"code":wrong}, ok=False, text="exit_code=1\nstderr:\nAssertionError", meta={"exit_code":1},
                     call_id="repeat", step=20)
        self.assertEqual(self.p.corrected_probe_support(), [support])
        self.assertTrue(self.p.blocked_reason())
        self.assertFalse(any(r["kind"] == "passed_check" for r in self.p.review_evidence().values()))

    def check(self, text, command="unittest discover", *, duration="1.123"):
        args = {"code": f"r = subprocess.run({command.split()!r}); print(r.stdout)"}
        self.observe(text="exit_code=0\nstdout:\n" + text.replace("$", duration), args=args)

    def test_same_pass_with_new_timing_advises_and_keeps_working(self):
        self.src.write_text("final")
        self.check("Ran 34 tests in $s\n\nOK")
        self.assertEqual(self.p.phase(self.budget), "work")
        self.check("0 0\n['Ran 34 tests in $s', 'OK']", duration="4.015")
        self.assertEqual(self.p.phase(self.budget), "work")
        self.assertIsNone(self.p.blocked_reason())
        self.assertEqual(self.p.evidence()["files"], ["module.py"])
        self.assertEqual(self.p.evidence()["check_signals"], ["unittest · 34 tests OK"])

    def test_nested_failure_is_preserved_until_same_suite_passes(self):
        self.check("returncode=1\n2 passed in 0.25s", "pytest group_a")
        self.check("12 passed in 1.55s", "pytest group_b")
        self.assertTrue(self.p.blocked_reason())
        self.src.write_text("fix")
        self.check("2 passed in 0.37s", "pytest group_a")
        self.assertIsNone(self.p.blocked_reason())
        self.assertEqual(self.p.phase(self.budget), "work")

    def test_zero_outer_exit_neither_verifies_nor_clears_a_failure(self):
        self.check("Ran 1 test in $s\n\nFAILED (failures=1)")
        self.observe(text="exit_code=0\nstdout:\nOK")
        self.assertTrue(self.p.blocked_reason())
        self.assertEqual(self.p.evidence()["check_signals"], [])

    def test_named_failures_in_one_inline_python_run_can_be_independently_reviewed(self):
        code = "from pathlib import Path\nvalue = Path('module.py').read_text()\n" \
               "print('FAIL quantity')\nprint('FAIL date')\nassert value == 'new'"
        output = "exit_code=1\nstdout:\nPASS opening\nFAIL quantity\nFAIL date\nstderr:\nAssertionError"
        self.observe(args={"code":code}, text=output, ok=False, meta={"exit_code":1}, call_id="named-failures")
        failures = self.p.failed_checks()
        self.assertEqual(len(failures), 2)
        probes = self.p.inline_failures()
        self.assertEqual(probes.keys(), failures.keys())
        self.assertEqual({probe["case_label"] for probe in probes.values()}, {"quantity", "date"})
        for key, probe in probes.items():
            self.assertEqual((probe["code"], probe["error"], probe["tool_call_id"]), (code, output, "named-failures"))
            self.assertEqual(self.p.review_evidence()["fail:" + key]["scope"], key)

    def test_subprocess_success_does_not_hide_the_outer_line_count_assertion(self):
        code = ('import subprocess\nfrom pathlib import Path\n'
                'r = subprocess.run(["python3", "verify.py"], capture_output=True, text=True)\n'
                'text = Path("proposal.md").read_text()\n'
                'assert r.returncode == 0\nassert text.count("\\n") + 1 == 917\n')
        failure = ('exit_code=1\nstdout:\nexit_code = 0\nPASS 200 / FAIL 0\n'
                   'stderr:\nTraceback (most recent call last):\n  File "<string>", line 6\nAssertionError')
        self.observe(args={"code":code},text=failure,ok=False,meta={"exit_code":1},call_id="outer-failed")
        original = self.p.failed_checks()
        self.assertTrue(original)
        probe, = self.p.inline_failures().values()
        self.assertEqual(probe["code"],code)
        self.assertEqual(probe["tool_call_id"],"outer-failed")
        fixed = code.replace('text.count("\\n") + 1','len(text.splitlines())')
        self.observe(args={"code":fixed},text="exit_code=0\nstdout:\nPASS 200 / FAIL 0",
                     meta={"exit_code":0},call_id="outer-corrected")
        self.assertEqual(self.p.failed_checks(),original)
        self.assertIn("不是文件 SHA-256",self.p.steering())
        self.assertIn("[BLOCKED]",self.p.steering())
        self.assertIn("process_id",self.p.steering())

    def test_repeated_other_passes_warn_without_waiving_the_original_failure(self):
        self.p = MemberProgress(self.root, ["delivery.txt"])
        (self.root/"delivery.txt").write_text("candidate")
        self.observe("write_file",args={"path":"delivery.txt"})
        self.observe(args={"code":"assert 1 == 2"},text="exit_code=1\nstderr:\nAssertionError",
                     ok=False,meta={"exit_code":1},call_id="failed")
        old = self.p.failed_checks()
        self.check("2 passed in 0.1s","pytest other")
        self.assertEqual(self.p.phase(self.budget),"work")
        self.check("2 passed in 0.2s","pytest other")
        self.assertEqual(self.p.phase(self.budget),"work")
        self.assertIn("[BLOCKED]",self.p.steering())
        self.assertEqual(self.p.failed_checks(),old)
        self.assertTrue(self.p.blocked_reason())

    def test_missing_deliverable_or_changed_candidate_keeps_real_work_available(self):
        self.p = MemberProgress(self.root,["delivery.txt"])
        self.observe(args={"code":"assert 1 == 2"},text="exit_code=1",ok=False,meta={"exit_code":1})
        for _ in range(2):
            self.check("2 passed in 0.1s","pytest other")
        self.assertEqual(self.p.phase(self.budget),"work")
        (self.root/"delivery.txt").write_text("new candidate")
        self.observe("write_file",args={"path":"delivery.txt"})
        self.assertEqual(self.p.phase(self.budget),"work")

    def test_unedited_truncated_or_changed_scripts_cannot_be_delegated_as_disclosed_probes(self):
        script = self.root / "verify.py"
        script.write_text("raise AssertionError('original check')\n")
        self.p = MemberProgress(self.root)
        args = {"command":'python3 verify.py; echo "EXIT=$?"'}
        failure = "exit_code=0\nstdout:\nPASS 191 / FAIL 9\nEXIT=1"
        self.observe("run_shell",failure,args=args,meta={"exit_code":0})
        self.assertFalse(self.p.inline_failures())  # No member edit has been observed.
        script.write_text("raise AssertionError('edited check')\n")
        self.observe("edit_file",args={"path":"verify.py"})
        self.observe("run_shell",failure,args=args,meta={"exit_code":0,"truncated":True})
        self.assertFalse(self.p.inline_failures())
        self.observe("run_shell",failure,args=args,meta={"exit_code":0})
        probe, = self.p.inline_failures().values()
        self.assertEqual(probe["exit_code"],1)
        self.assertIn("edited check",probe["code"])
        self.assertTrue(self.p.failed_checks())

    def test_edited_checker_pass_does_not_clear_its_original_failure_without_peer_review(self):
        script = self.root / "test_verify.py"
        original = "assert 1 == 2, 'original business requirement'\n"
        script.write_text(original)
        self.observe("write_file",args={"path":"test_verify.py"})
        args = {"command":"python3 test_verify.py"}
        self.observe("run_shell","exit_code=1\nstderr:\nAssertionError",args=args,ok=False,meta={"exit_code":1})
        failed = self.p.failed_checks()
        self.assertTrue(failed)
        script.write_text("assert True\nprint('PASS weakened check')\n")
        self.observe("edit_file",args={"path":"test_verify.py"})
        self.observe("run_shell","exit_code=0\nstdout:\nPASS weakened check",args=args,meta={"exit_code":0})
        self.assertEqual(self.p.failed_checks(),failed)
        self.assertEqual(next(iter(self.p.inline_failures().values()))["code"],original)

    def test_probe_capacity_keeps_original_failures_and_reports_undisclosed_scope(self):
        codes = [f"assert measured() == {i}" for i in range(17)]
        for i,code in enumerate(codes):
            self.observe(args={"code":code},text="exit_code=1",ok=False,
                         meta={"exit_code":1},call_id=f"original-{i}")
        retained = self.p.inline_failures()
        self.assertEqual(len(retained),16)
        self.assertEqual(len(self.p.failed_checks()),17)
        self.assertEqual({p["tool_call_id"] for p in retained.values()}, {f"original-{i}" for i in range(16)})
        overflow, = self.p.evidence()["undisclosed_failure_probes"]
        self.assertEqual(overflow["tool_call_id"],"original-16")
        self.assertIn(overflow["scope_id"],self.p.failed_checks())
        self.observe(args={"code":codes[0]},text="exit_code=1",ok=False,
                     meta={"exit_code":1},call_id="repeated")
        self.assertEqual(self.p.inline_failures(),retained)
        self.observe(args={"code":codes[0]},text="exit_code=0\nstdout:\nPASS measured value",
                     meta={"exit_code":0},call_id="resolved")
        self.assertEqual(len(self.p.inline_failures()),15)
        self.observe(args={"code":codes[16]},text="exit_code=1",ok=False,
                     meta={"exit_code":1},call_id="retried-after-space")
        self.assertEqual(len(self.p.inline_failures()),16)
        self.assertEqual(self.p.evidence()["undisclosed_failure_probes"],[])

    def test_long_browser_failure_keeps_full_original_evidence_with_bounded_limits(self):
        code = "assert not js_errors\n" + "# diagnostic setup\n" * 360
        error = "exit_code=1\nstdout:\n" + "checked current page\n" * 50 + "stderr:\nNameError: js_errors"
        self.assertGreater(len(code.encode()), 6000)
        self.observe("run_browser", error, args={"code":code}, ok=False,
                     meta={"exit_code":1}, call_id="original-browser")
        scope, = self.p.failed_checks()
        self.assertEqual(self.p.inline_failures()[scope]["code"],code)
        self.assertEqual(self.p.inline_failures()[scope]["error"],error)
        self.observe("run_browser", error, args={"code":code}, ok=False,
                     meta={"exit_code":1}, call_id="repeat-browser")
        self.assertEqual(self.p.inline_failures()[scope]["tool_call_id"],"original-browser")
        for oversized, output, meta in [
                (code + "#" * 32768, error, {"exit_code":1}),
                (code, error + "x" * 32768, {"exit_code":1}),
                (code, error, {"exit_code":1,"truncated":True}),
                (code, error, {"exit_code":1,"timed_out":True}),
                (code, error, {"exit_code":1,"cancelled":True}),
                (code, error, {})]:
            with self.subTest(meta=meta, code_bytes=len(oversized), error_bytes=len(output)):
                self.p = MemberProgress(self.root)
                self.observe("run_browser", output, args={"code":oversized}, ok=False, meta=meta)
                if not meta.get('timed_out') and not meta.get('cancelled'):
                    self.assertTrue(self.p.failed_checks())
                self.assertFalse(self.p.evidence()['check_signals'])
                self.assertFalse(self.p.inline_failures())

    def test_stale_checks_require_rerun_after_modification(self):
        self.check("1 passed in 0.12s", "pytest")
        self.src.write_text("new contents")
        self.observe(text="exit_code=0")
        self.assertFalse(self.p.evidence()["check_signals"])
        self.check("1 passed in 0.34s", "pytest")
        self.assertEqual(self.p.phase(self.budget), "work")
        # Identical bytes written by a test fixture do not create a new version.
        self.src.write_text("new contents")
        self.check("1 passed in 0.35s", "pytest")
        self.assertEqual(self.p.phase(self.budget), "work")

    def test_missing_required_file_is_not_inferred_from_test_output(self):
        self.p = MemberProgress(self.root, ["report.md"])
        for i in range(8):
            self.check("5 passed in 0.1s", "pytest")
        self.assertEqual(self.p.phase(self.budget), "work")
        self.assertIn("report.md", self.p.blocked_reason())
        self.assertIn("report.md", self.p.steering())
        self.assertIn("进度提醒", self.p.steering())

    def test_pagination_advances_and_repeated_inspection_reports_the_observed_tools(self):
        for i in range(10):
            self.observe("read_file", meta={"path": "module.py", "offset": 100 * i})
            self.assertEqual(self.p.phase(self.budget), "work")
        for i in range(7):
            self.observe(args={"code": f"print(open('module.py').read()[{i}:])"})
        self.assertEqual(self.p.phase(self.budget), "work")
        self.assertIsNone(self.p.blocked_reason())
        self.assertEqual(self.p.evidence()["inspected"], ["module.py"])

    def test_recovery_requires_matching_operation_result_and_no_fresh_work(self):
        def observe(code, result):
            self.observe(args={"code": code}, text=result, meta={"exit_code": 0})
            return self.p.recovery_feedback()

        for i in range(5):
            self.assertFalse(observe(f"print({i})", "same stdout"))
        for i in range(5):
            self.assertFalse(observe("print(1)", f"distinct stdout {i}"))
        for i in range(4):
            self.src.write_text(f"published work {i}")
            self.assertFalse(observe("print(1)", "same stdout"))
        self.assertFalse(observe("print(1)", "same stdout"))
        self.assertIn("3 次", observe("print(1)", "same stdout"))
        self.assertEqual(self.p.phase(self.budget), "work")
        self.assertEqual(self.p.evidence()["stop_reason"], "")
        self.assertFalse(observe("print(2)", "new stdout"))

    def test_genuine_small_python_edits_report_versions_outputs_and_missing_paths(self):
        self.p = MemberProgress(self.root, ["guide.md", "review.md"])
        guide = self.root / "guide.md"
        lines = ["保留正文。" * 80 + "可压缩" * 6 for _ in range(3)]
        guide.write_text("\n".join(lines))
        self.observe(text="first draft", meta={"exit_code": 0})
        before = guide.stat().st_size
        for i in range(1, 6):
            # One command replaces words on distant lines. Line-aligned changes
            # remain small even when they span nearly the whole document.
            lines = [line.replace("可压缩", "略", 1) for line in lines]
            guide.write_text("\n".join(lines))
            self.p.project_versions()  # A publisher may have primed the hash cache.
            self.observe(text=f"exit_code=0\nstdout:\nunits left {6000 - i}", meta={"exit_code": 0})
            self.assertEqual(self.p.phase(self.budget), "work")
        note = self.p.steering()
        for fact in ("run_python", "guide.md", "5 次局部小改", "units left 5999", "units left 5995",
                     f"{before} 到 {guide.stat().st_size} 字节", "review.md"):
            self.assertIn(fact, note)
        evidence = self.p.evidence()
        self.assertEqual(evidence["files"], ["guide.md"])
        self.assertEqual(evidence["missing_required_files"], ["review.md"])
        self.assertEqual(evidence["check_signals"], [])
        self.assertEqual(evidence["stop_reason"], "")
        self.assertNotIn("units left", json.dumps(evidence))

    def test_edit_advisory_clears_on_material_change_new_file_and_fresh_page(self):
        guide = self.root / "guide.md"
        for activity in ("rewrite", "new_file", "read_page"):
            with self.subTest(activity=activity):
                self.p = MemberProgress(self.root, ["guide.md"])
                guide.write_text("a" * 4000)
                self.observe("write_file", meta={"path":"guide.md"})
                for i in range(1, 6):
                    guide.write_text("b" * i + "a" * (4000 - i))
                    self.observe(text=f"revision {i}", meta={"exit_code": 0})
                self.assertIn("局部小改", self.p.steering())
                if activity == "rewrite":
                    guide.write_text("k" * 4000)  # Equal size, different throughout.
                    self.observe(text="rewritten", meta={"exit_code": 0})
                elif activity == "new_file":
                    (self.root / "other.txt").write_text("additional output")
                    self.observe(text="written", meta={"exit_code": 0})
                else:
                    for offset in range(0, 100, 20):
                        self.observe("read_file", meta={"path":"guide.md", "offset":offset})
                        self.assertNotIn("局部小改", self.p.steering())
                self.assertEqual(self.p.phase(self.budget), "work")
                self.assertEqual(self.p.stall_feedback(), "")

    def test_round_robin_pages_record_versioned_union_and_soft_hint(self):
        data = "".join(f"{n:04d} 测试-" + "x" * 30 + "\r\n" for n in range(600))
        self.src.write_bytes(data.encode())
        self.p = MemberProgress(self.root)
        ctx = ToolContext(Sandbox(self.root), Limits())
        tools = build_default_registry()

        def read(tool, **args):
            result = tools.dispatch(tool, {"path":"module.py", **args}, ctx)
            self.assertTrue(result.ok, result.content[:100])
            self.observe(tool, result.content, meta=result.meta)
            return self.p.read_checkpoint()["module.py"]

        for start, count in [(1,160), (161,300), (461,140)]:
            receipt = read("read_lines", start_line=start, limit=count)
            self.assertNotIn("已返回过", self.p.steering())
        self.assertEqual(receipt["line_ranges"], [(1,600)])
        self.assertEqual(receipt["char_ranges"], [(0,len(data))])
        self.assertTrue(receipt["complete"])
        for start in (1,161,461):
            receipt = read("read_lines", start_line=start, limit=80)
            self.assertIn("定向读取", self.p.steering())
            self.assertIn("全文", self.p.steering())
            self.assertEqual(self.p.phase(self.budget), "work")
        read("read_file", offset=2, limit=15)
        self.assertIn("已返回过", self.p.steering())

        self.src.write_text("second revised source line\n" * 10)
        self.observe("write_file", meta={"path":"module.py"})
        self.assertNotIn("module.py", self.p.read_checkpoint())
        revised = read("read_file", offset=0, limit=30)
        self.assertNotEqual(revised["sha256"],receipt["sha256"])
        self.assertFalse(revised["complete"])
        self.assertEqual(revised["line_ranges"], [])
        read("read_lines", start_line=2, limit=9)
        self.assertTrue(self.p.read_checkpoint()["module.py"]["complete"])

    def test_read_file_pages_cover_full_unicode_contents_and_clipped_line_does_not(self):
        self.src.write_text("测" * 150000)
        self.p = MemberProgress(self.root)
        ctx = ToolContext(Sandbox(self.root), Limits())
        tools = build_default_registry()
        def read(tool, **args):
            result = tools.dispatch(tool, {"path":"module.py", **args}, ctx)
            self.assertTrue(result.ok, result.content[:100])
            self.observe(tool, result.content, meta=result.meta)
            return self.p.read_checkpoint()["module.py"]
        partial = read("read_lines")
        self.assertEqual(partial["partial_line"],1)
        self.assertFalse(partial["complete"])
        self.assertEqual(partial["line_ranges"],[])
        offset = 0
        while offset is not None:
            receipt = read("read_file",offset=offset,limit=6500)
            offset = receipt["next_offset"]
        self.assertTrue(receipt["complete"])
        self.assertEqual(receipt["char_ranges"], [(0,150000)])
        read("read_lines")
        self.assertIn("已返回过",self.p.steering())

    def test_dry_listing_is_advisory_for_a_verbal_assignment(self):
        self.assertIsNone(self.p.blocked_reason())
        for i in range(7):
            self.observe("list_dir")
        self.assertEqual(self.p.phase(self.budget), "work")
        self.assertIsNone(self.p.blocked_reason())
        self.assertIn("list_dir", self.p.stall_feedback())

    def test_source_dump_of_passing_output_is_not_a_test(self):
        for i in range(3):
            self.observe(text="exit_code=0\nstdout:\nRan 9 tests in 0.9s\nOK",
                         args={"code": "print(Path('module.py').read_text())"})
        self.assertEqual(self.p.evidence()["check_signals"], [])

    def test_reserves_a_final_call_and_keeps_failure(self):
        self.check("Ran 1 test in $s\nFAILED (errors=1)")
        self.budget.steps_left = 1
        self.assertEqual(self.p.phase(self.budget), "handoff")
        self.assertTrue(self.p.blocked_reason())

    def test_returncode_in_a_positional_print_beats_positive_text(self):
        self.observe(text="exit_code=0\nstdout:\n1 47\n['Ran 34 tests in 1.1s', 'OK']",
                     args={"code": "r = subprocess.run(['python','-m','unittest']); print(r.returncode, 47)"})
        self.assertTrue(self.p.blocked_reason())
        self.assertEqual(self.p.evidence()["check_signals"], [])

    def test_nested_traceback_and_expected_http_failures_are_distinct(self):
        self.check('Traceback (most recent call last):\n  File "tests.py", line 7, in <module>\nAssertionError: stale')
        self.assertTrue(self.p.blocked_reason())
        self.check('POST /example 409 Conflict\nRan 12 tests in 0.4s\nOK')
        self.assertIsNone(self.p.blocked_reason())

    def test_a_later_write_in_the_same_step_invalidates_the_earlier_pass(self):
        self.src.write_text("new source after test")
        check = Observation(step_id="s", source="run_python", ok=True, content="exit_code=0\n3 passed in 0.14s")
        write = Observation(step_id="s", source="write_file", ok=True, content="done")
        self.p.observe(StepRecord(step=1, observations=[check, write]), RAOState(goal="g", task_id="t"))
        self.assertEqual(self.p.evidence()["files"], ["module.py"])
        self.assertEqual(self.p.evidence()["check_signals"], [])

    def test_independent_acceptance_rejection_reopens_repair_once(self):
        self.src.write_text("initial implementation")
        for i in range(2):
            self.check("Ran 3 tests in 0.26s\nOK")
        self.assertEqual(self.p.phase(self.budget), "work")
        self.assertEqual(self.p.phase(self.budget, "[验收未通过] wrong record"), "work")
        self.assertFalse(self.p.evidence()["check_signals"])
        self.src.write_text("repair")
        self.observe("write_file")
        self.assertEqual(self.p.phase(self.budget, "[验收未通过] wrong record"), "work")

    def test_direct_assertion_failure_survives_unrelated_command(self):
        script = {"code": "x = int(open('data').read()); assert x > 0"}
        self.observe(text="exit_code=1\nstderr:\nTraceback (most recent call last):\n"
                          '  File "<string>", line 1, in <module>\nAssertionError',
                     args=script, ok=False, meta={"exit_code": 1})
        self.check("4 passed in 0.21s", "pytest other")
        self.assertIn("断言", self.p.blocked_reason())
        self.observe(text="exit_code=0\n（无输出）", args=script, meta={"exit_code": 0})
        self.assertIsNone(self.p.blocked_reason())
        self.assertEqual(self.p.evidence()["check_signals"], ["pytest · 4 passed"])

    def test_reported_expected_error_with_successful_suite(self):
        self.check("negative case expected returncode=1\n1 passed in 0.03s", "pytest")
        self.assertIsNone(self.p.blocked_reason())
        self.assertTrue(self.p.evidence()["check_signals"])

    def test_custom_smoke_checks_survive_document_writes_as_prior_observations(self):
        self.observe(text="exit_code=0\nstdout:\nPASS  adjacent interval  -> 201\nPASS  overlap rejected -> 409\n",
                     args={"code": "subprocess.run(['python', 'smoke_test.py'])"}, meta={"exit_code": 0})
        current = self.p.evidence()["check_signals"]
        self.assertIn("2 项 PASS", current[0])
        (self.root/"notes.md").write_text("Documented limitations")
        self.observe("write_file")
        receipt = self.p.evidence()
        self.assertEqual(receipt["prior_check_signals_before_edits"], current)
        self.assertEqual(receipt["check_signals"], [])
        self.assertEqual(receipt["executions"], {"run_python_calls": 1, "run_shell_calls": 0,
                                                  "run_browser_calls": 0, "outer_exit_zero": 1})

    def test_custom_fail_overrides_reassuring_individual_results(self):
        self.check("PASS  case one\nFAIL  case two\n", "python smoke_test.py")
        self.assertIn("case two", self.p.blocked_reason())
        self.assertFalse(self.p.evidence()["check_signals"])

    def test_printing_a_claim_or_reading_a_dynamic_file_is_not_a_check(self):
        for code in ["print('PASS  everything')", "p = 'module.py'; print(open(p).read())"]:
            self.observe(text="exit_code=0\nstdout:\nPASS  everything", args={"code": code})
        self.assertFalse(self.p.evidence()["check_signals"])

    def test_first_green_steers_to_exact_remaining_member_deliverables(self):
        self.p = MemberProgress(self.root, ["guide.md", "module.py"])
        self.check("2 passed in 0.1s", "pytest")
        self.assertEqual(self.p.phase(self.budget), "work")
        self.assertEqual(self.p.evidence()["missing_required_files"], ["guide.md"])
        self.assertIn("本次分工还需交付 guide.md", self.p.steering())

    def test_exact_repetition_reports_the_operation_and_the_still_missing_file(self):
        self.p = MemberProgress(self.root, ["notes/research.md"])
        for _ in range(12):
            self.observe("read_lines", "source content", args={"path": "module.py"}, meta={"path": "module.py"})
        self.assertEqual(self.p.phase(self.budget), "work")
        advice = self.p.steering()
        for text in ("read_lines module.py", "source content", "notes/research.md", "进度提醒"):
            self.assertIn(text, advice)
        self.assertEqual(self.p.evidence()["stop_reason"], "")
        self.assertTrue(self.p.blocked_reason())

    def test_repeated_distinct_source_pages_get_real_writing_turn_then_verification(self):
        self.p = MemberProgress(self.root, ["README.md", "checks.md"])
        self.check("PASS  real smoke test returned 200", "python check_test.py")
        for i in range(4):
            self.observe("read_file", meta={"path": "module.py", "offset": 5000*i})
        self.assertEqual(self.p.phase(self.budget), "work")
        self.assertEqual(self.p.pending_paths(), {"README.md", "checks.md"})
        self.assertIn("常规工具", self.p.steering())
        self.assertIn("real smoke test", self.p.steering())
        (self.root / "README.md").write_text("What actually ran")
        self.observe("write_file")
        self.assertEqual(self.p.phase(self.budget), "work")
        self.assertEqual(self.p.pending_paths(), {"checks.md"})
        self.assertTrue(self.p.evidence()["prior_check_signals_before_edits"])
        self.assertIn("checks.md", self.p.blocked_reason())
        (self.root / "checks.md").write_text("One check covered")
        self.observe("write_file")
        self.assertIsNone(self.p.blocked_reason())

    def test_dynamic_research_extracts_advance_only_on_new_archived_text(self):
        paragraphs = [f"Section {i}. Durable, identifiable research content extracted from the original page."
                      for i in range(10)]
        archive = json.dumps({"kind": "fetched_source", "text": "\n".join(paragraphs)})
        path = "sources/" + hashlib.sha256(archive.encode()).hexdigest() + ".json"
        (self.root/"sources").mkdir()
        (self.root/path).write_text(archive)
        self.p = MemberProgress(self.root, ["notes/research.md"])
        args = {"code": "path = next(Path('sources').glob('*.json')); print(json.loads(path.read_text())['text'])"}
        for line in paragraphs:
            self.observe("run_python", "exit_code=0\nstdout:\n" + line, args=args, meta={"exit_code":0})
            self.assertEqual(self.p.phase(self.budget), "work")
        for i in range(7):
            self.observe("run_python", f"exit_code=0\nstdout:\nvariant-{i}\n" + paragraphs[-1],
                         args=args, meta={"exit_code":0})
        self.assertIn("run_python", self.p.stall_feedback())
        self.assertIn("notes/research.md", self.p.steering())
        self.assertEqual(self.p.phase(self.budget), "work")
        self.assertTrue(self.p.blocked_reason())
        self.assertEqual(self.p.evidence()["check_signals"], [])

    def test_read_source_uses_body_coordinates_independent_of_raw_archive_pages(self):
        archive = json.dumps({"kind":"fetched_source","text":"a source with several pages"})
        path = "sources/" + hashlib.sha256(archive.encode()).hexdigest() + ".json"
        (self.root/"sources").mkdir()
        (self.root/path).write_text(archive)
        self.p = MemberProgress(self.root)
        ctx = ToolContext(Sandbox(self.root), Limits())
        tools = build_default_registry()
        def read(tool, **params):
            args = {"path":path, **params}
            result = tools.dispatch(tool, args, ctx)
            self.assertTrue(result.ok, result.content)
            self.observe(tool, result.content, args=args, meta=result.meta)
        read("read_file")
        read("read_source", limit=3)
        receipt = self.p.read_checkpoint()[path]
        self.assertTrue(receipt["complete"])
        self.assertEqual(receipt["source_body"]["char_ranges"], [(0,3)])
        self.assertFalse(receipt["source_body"]["complete"])
        self.assertNotIn("已返回过", self.p.steering())
        read("read_source", offset=1, limit=2)
        self.assertIn("已返回过", self.p.steering())
        self.assertEqual(self.p.evidence()["inspected"], [path])
        self.assertEqual(self.p.evidence()["check_signals"], [])

    def test_source_discovery_counts_queries_instead_of_retrieval_timestamps(self):
        for query, path in [("multiagent", "a"), ("multiagent", "b"), ("interoperability", "c")]:
            self.observe("search_sources", args={"query":query,"collection":"papers"}, meta={"path":path})
        self.assertEqual(self.p.evidence()["distinct_searches"], 2)

    def test_stalled_writes_never_override_reserves_or_binary_work(self):
        for paths, steps in [(["data.sqlite3","screen.png"], 30), (["doc.md"], 1)]:
            with self.subTest(paths=paths):
                self.p = MemberProgress(self.root, paths)
                for _ in range(7):
                    self.observe("list_dir")
                self.assertEqual(self.p.phase(self.budget.model_copy(update={"steps_left":steps})),
                                 "handoff" if steps == 1 else "work")

    def test_recovered_draft_can_write_before_any_new_inspection(self):
        self.p = MemberProgress(self.root, ["report.md", "data.db"])
        self.p.resume_delivery()
        self.assertEqual(self.p.phase(self.budget), "work")
        self.assertEqual(self.p.pending_paths(), {"report.md", "data.db"})
        self.budget.steps_left = 1
        self.assertEqual(self.p.phase(self.budget), "handoff")
        self.assertIn("data.db", self.p.blocked_reason())

    def test_budget_leaves_an_actual_writing_opportunity_before_final(self):
        self.p = MemberProgress(self.root, ["report.md"])
        self.observe("list_dir")
        self.budget.steps_left = 3
        self.assertEqual(self.p.phase(self.budget), "work")
        self.assertTrue(self.p.blocked_reason())
        self.budget.steps_left = 1
        self.assertEqual(self.p.phase(self.budget), "handoff")

    def test_binary_only_missing_does_not_switch_to_text_writers(self):
        self.p = MemberProgress(self.root, ["data.sqlite3"])
        self.p.resume_delivery()
        for i in range(5):
            self.observe("read_file", meta={"path":"module.py", "offset":i})
        self.assertEqual(self.p.phase(self.budget), "work")

    def test_transferred_custom_check_is_only_cleared_by_same_case_and_suite(self):
        self.check("FAIL  total = 8  7", "python acceptance_test.py")
        failed = self.p.failed_checks()
        self.assertTrue(failed)
        self.assertNotIn("acceptance_test.py", repr(failed))
        self.p = MemberProgress(self.root)
        self.p.restore_failed_checks(failed)
        self.check("PASS  application loaded  200", "python acceptance_test.py")
        self.check("PASS  total = 8  8", "python other_test.py")
        self.check("PASS  total = 7  7", "python acceptance_test.py")
        self.assertTrue(self.p.blocked_reason())
        self.check("PASS  total = 8  8", "python acceptance_test.py")
        self.assertFalse(self.p.blocked_reason())

    def test_real_shell_runner_case_survives_output_filters_and_draft_transfer(self):
        strip = r"sed -e 's/\x1b\[[0-9;]*m//g'"
        first = ("python3 selftest.py 2>&1 | " + strip + " | tail -45; echo '---'; "
                 "python3 selftest.py >/tmp/st.log 2>&1; echo \"EXIT=$?\"; "
                 + strip + " /tmp/st.log | grep -E 'FAIL|结果'")
        final = ("python3 selftest.py >/tmp/final.log 2>&1; RC=$?; "
                 + strip + " /tmp/final.log | grep -E 'FAIL|重复取消|取消不存在|结果'; echo \"EXIT=$RC\"")
        self.observe("run_shell", "exit_code=0\nstdout:\nFAIL 重复取消被拒(400)  -> HTTP 500 / 旧错误"
                     "\nFAIL 取消不存在的预约(404)  -> HTTP 500\nEXIT=1", args={"command":first}, meta={"exit_code":0})
        failed = self.p.failed_checks()
        self.assertEqual(len(failed), 2)
        self.p = MemberProgress(self.root)
        self.p.restore_failed_checks(failed)
        self.observe("run_shell", "exit_code=0\nstdout:\n  PASS 重复取消被拒(400)  -> HTTP 400 / 新诊断\nEXIT=0",
                     args={"command":final}, meta={"exit_code":0})
        self.assertEqual(list(self.p.failed_checks().values()), ["工具输出 FAIL · 取消不存在的预约(404)  -> HTTP 500"])
        self.observe("run_shell", "exit_code=0\nstdout:\n  \x1b[32mPASS\x1b[0m 取消不存在的预约(404)  -> HTTP 404",
                     args={"command":"python3 selftest.py"}, meta={"exit_code":0})
        self.assertFalse(self.p.failed_checks())

    def test_shell_custom_case_identity_resists_false_resolution(self):
        case = "cancel missing (404)"; command = "python3 check.py --profile local"
        self.observe("run_shell", "exit_code=0\nstdout:\nFAIL " + case + "  -> HTTP 500",
                     args={"command":command, "cwd":"frontend"}, meta={"exit_code":0})
        initial = self.p.failed_checks()
        for invocation, cwd, caption, exit_footer in (
                (command, "backend", case, ""),
                (command.replace("local","remote"), "frontend", case, ""),
                (command.replace("check.py","different.py"), "frontend", case, ""),
                ("grep -E PASS /tmp/old.log", "frontend", case, ""),
                (command + " >/tmp/new.log; grep -E PASS /tmp/old.log", "frontend", case, ""),
                (command + "; echo 'PASS cancel missing (404)'", "frontend", case, ""),
                (command + " | sed -e 's/FAIL/PASS/g'", "frontend", case, ""),
                (command + "; python3 other.py", "frontend", case, ""),
                (command + " | tail -5", "frontend", "cancel missing (403)", ""),
                (command + " | tail -5; echo \"EXIT=$?\"", "frontend", case, "\nEXIT=1")):
            with self.subTest(invocation=invocation, cwd=cwd):
                self.observe("run_shell", "exit_code=0\nstdout:\nPASS " + caption + "  -> HTTP 404" + exit_footer,
                             args={"command":invocation,"cwd":cwd},meta={"exit_code":0})
                self.assertTrue(initial.keys() <= self.p.failed_checks().keys())
        self.observe("run_shell","exit_code=0\nstdout:\nPASS " + case + "  -> HTTP 404",
                     args={"command":command+" | tail -5","cwd":"frontend"},meta={"exit_code":0})
        self.assertTrue(initial.keys().isdisjoint(self.p.failed_checks()))

    def test_case_keys_preserve_full_captions_independently_of_display_limit(self):
        prefix = "x" * 180
        command = {"command":"python3 checks.py"}
        self.observe("run_shell", "exit_code=0\nstdout:\nFAIL " + prefix + "A  -> mismatch\nFAIL "
                     + prefix + "B  -> mismatch", args=command, meta={"exit_code":0})
        self.assertEqual(len(self.p.failed_checks()), 2)
        self.observe("run_shell", "exit_code=0\nstdout:\nPASS " + prefix + "A  -> expected",
                     args=command, meta={"exit_code":0})
        self.assertEqual(len(self.p.failed_checks()), 1)

    def test_transferred_standard_suite_failure_requires_its_actual_rerun(self):
        self.check("1 failed in 0.1s", "pytest group_a")
        previous = self.p.failed_checks()
        self.p = MemberProgress(self.root)
        self.p.restore_failed_checks(previous)
        self.check("1 passed in 0.1s", "pytest group_b")
        self.assertTrue(self.p.blocked_reason())
        self.check("1 passed in 0.1s", "pytest group_a")
        self.assertFalse(self.p.blocked_reason())

    def test_existing_implementation_and_four_new_pages_preserve_editing_and_verification(self):
        app = self.root / "app.py"
        app.write_text("# existing published source\n" * 1000)
        (self.root/"README.md").write_text("previous version")
        (self.root/"history.db").write_bytes(b"existing historical input")
        paths = ["app.py", "README.md", "checks.md"]
        self.p = MemberProgress(self.root, required_paths=paths)
        for i in range(4):
            self.observe("read_file", meta={"path":"app.py","offset":5000*i})
            self.assertEqual(self.p.phase(self.budget), "work")
        self.assertEqual(self.p.evidence()["files"], [])
        self.assertEqual(self.p.evidence()["unchanged_required_files"], ["README.md","app.py"])
        self.assertEqual(self.p.evidence()["missing_required_files"], ["checks.md"])
        self.assertEqual(self.p.evidence()["executions"]["run_python_calls"], 0)
        self.assertIn("编辑实现、运行验证", self.p.steering())
        app.write_text("complete implementation")
        self.observe("write_file")
        self.check("3 passed in 0.23s", "pytest suite")
        self.assertEqual(self.p.phase(self.budget), "work")
        self.assertEqual(self.p.evidence()["files"], ["app.py"])

    def test_private_repair_compares_actual_content_to_public_assignment_baseline(self):
        import hashlib
        base = {self.src.name:hashlib.sha256(self.src.read_bytes()).hexdigest()}
        self.p = MemberProgress(self.root, ["module.py","checks.md"], public_baseline=base)
        self.p.resume_delivery()
        self.assertEqual(self.p.phase(self.budget),"work")
        self.assertFalse(self.p.evidence()["files"])
        self.src.write_text("private repair still to verify")
        self.p = MemberProgress(self.root, ["module.py","checks.md"], public_baseline=base)
        self.p.resume_delivery()
        self.assertEqual(self.p.phase(self.budget),"work")
        self.assertEqual(self.p.evidence()["files"], ["module.py"])
        self.assertFalse(self.p.evidence()["check_signals"])

    def test_shell_changes_are_seen_and_true_suite_failure_is_scoped(self):
        self.src.write_text("exported by a shell tool")
        self.observe("run_shell", "exit_code=1\nstdout:\n1 failed in 0.23s",
                     args={"command":"pytest tests/a.py"}, ok=False, meta={"exit_code":1})
        self.assertEqual(self.p.evidence()["files"], ["module.py"])
        self.observe("run_shell", "exit_code=0\nstdout:\n2 passed in 0.2s",
                     args={"command":"pytest tests/b.py"}, meta={"exit_code":0})
        self.assertTrue(self.p.blocked_reason())
        self.observe("run_shell", "exit_code=0\nstdout:\n1 passed in 0.2s",
                     args={"command":"pytest tests/a.py"}, meta={"exit_code":0})
        self.assertFalse(self.p.blocked_reason())
        self.assertEqual(self.p.evidence()["executions"]["run_shell_calls"], 3)

    def test_shell_source_dump_is_not_a_test_and_navigation_can_continue(self):
        for i in range(9):
            self.observe("read_lines", "some source", args={"path":"module.py","start_line":12*i+1})
            self.assertEqual(self.p.phase(self.budget),"work")
            self.observe("find_files","module.py",args={"pattern":f"module{i}*.py"})
            self.assertEqual(self.p.phase(self.budget),"work")
            self.observe("search_files",f"matched text {i}",args={"query":str(i)})
        self.observe("run_shell","exit_code=0\nstdout:\nRan 23 tests in 0.4s\nOK",
                     args={"command":"cat module.py"},meta={"exit_code":0})
        self.assertFalse(self.p.evidence()["check_signals"])
        self.assertEqual(self.p.evidence()["distinct_searches"],18)
        self.assertEqual(self.p.evidence()["inspected"],["module.py"])

    def test_distinct_shell_query_results_preserve_work(self):
        for i in range(9):
            self.observe("run_shell",f"exit_code=0\nstdout:\nrow {i}",args={"command":"sqlite3 data.db SELECT"},
                         meta={"exit_code":0})
            self.assertEqual(self.p.phase(self.budget),"work")

    def test_two_passes_from_untouched_public_project_do_not_force_implementation_closed(self):
        for i in range(2):
            self.observe("run_shell","exit_code=0\nstdout:\n2 passed in 0.2s",args={"command":"pytest"},
                         meta={"exit_code":0})
        self.assertEqual(self.p.phase(self.budget),"work")
        self.assertFalse(self.p.evidence()["files"])
        self.observe("run_shell","exit_code=0\nstdout:\n99 passed in 0.2s",
                     args={"command":"cat tests/report.txt"},meta={"exit_code":0})
        self.assertEqual(self.p.evidence()["check_signals"],["pytest · 2 passed"])

    def test_new_shell_navigation_queries_are_distinct_work(self):
        for i in range(8):
            self.observe("run_shell",f"exit_code=0\nstdout:\nmodule.py:{i}:item",args={"command":f"rg needle{i} ."},
                         meta={"exit_code":0})
            self.assertEqual(self.p.phase(self.budget),"work")
        self.assertEqual(self.p.evidence()["distinct_searches"],8)

    def test_deleting_an_assigned_obsolete_artifact_is_an_observed_change(self):
        self.src.unlink()
        self.observe("delete_file")
        self.p.require_handoff("close")
        self.assertIsNone(self.p.blocked_reason())
        self.assertEqual(self.p.evidence()["removed_files"],["module.py"])

    def test_incomplete_inline_trace_exposes_unavailable_peer_evidence(self):
        self.observe("run_browser","truncated",ok=False,args={"code":"assert page.title() == 'Ready'"},
                     meta={"exit_code":1,"stderr_bytes":40000,"truncated":True},call_id="lost-trace")
        item, = self.p.evidence()["undisclosed_failure_probes"]
        self.assertEqual((item["tool_call_id"],item["stderr_bytes"]),("lost-trace",40000))
        self.assertFalse(self.p.inline_failures())
        self.assertTrue(self.p.failed_checks())

    def test_browser_execution_tracks_project_changes_and_scopes_assertions(self):
        nav = "page.goto('http://localhost:8000')\nprint(page.locator('body').inner_text())"
        self.src.write_text("Updated app data during browser operation")
        self.observe("run_browser", "exit_code=0\nstdout:\nPASS  every page passed", args={"code":nav},
                     meta={"exit_code":0})
        self.assertEqual(self.p.evidence()["files"],["module.py"])
        self.assertEqual(self.p.evidence()["check_signals"],[])
        check = nav + "\nexpect(page.get_by_role('heading')).to_have_text('Ledger')"
        self.observe("run_browser", "exit_code=1", args={"code":check}, ok=False, meta={"exit_code":1})
        self.assertTrue(self.p.blocked_reason())
        self.observe("run_browser", "exit_code=0", args={"code":check,"cwd":"subproject"}, meta={"exit_code":0})
        self.assertTrue(self.p.blocked_reason())
        self.observe("run_browser", "exit_code=0", args={"code":nav+"\nassert page.title() == 'Ledger'"},
                     meta={"exit_code":0})
        self.assertTrue(self.p.blocked_reason())
        self.observe("run_browser", "exit_code=0\nstdout:\nFAIL page body", args={"code":check}, meta={"exit_code":0})
        self.assertIsNone(self.p.blocked_reason())
        self.assertIn("含浏览器断言的脚本退出 0", self.p.evidence()["check_signals"])
        self.assertEqual(self.p.evidence()["executions"]["run_browser_calls"],5)

    def test_each_unresolved_browser_scope_retains_one_replay_until_that_scope_passes(self):
        registry = build_default_registry()
        self.p.register_browser_replay(registry)
        ctx = ToolContext(Sandbox(self.root), Limits())
        probes = [{"code": f"assert page.title() == 'Case {i}'", "cwd":"."} for i in range(17)]
        for i, args in enumerate(probes):
            self.observe("run_browser", args=args, ok=False, meta={"exit_code":1}, call_id=f"original-{i}")
        for i in range(40):
            self.observe("run_browser", args=probes[0], ok=False, meta={"exit_code":1}, call_id=f"retry-{i}")
        ids = {f"original-{i}" for i in range(len(probes))}
        self.assertEqual(set(self.p.browser_replays()), ids)
        self.assertEqual(len(self.p.failed_checks()), 17)

        with patch.object(registry.get("run_browser"), "fn", return_value=ToolResult.success("ok", exit_code=0)) as run:
            result = registry.dispatch("rerun_browser_check", {"tool_call_id":"original-0"}, ctx)
        run.assert_called_once_with(ctx, probes[0])
        self.observe("rerun_browser_check", result.content, args={"tool_call_id":"original-0"},
                     ok=result.ok, meta=result.meta, call_id="fresh-receipt")
        self.assertEqual(set(self.p.browser_replays()), ids - {"original-0"})
        self.assertEqual(len(self.p.failed_checks()), 16)
        self.assertFalse(registry.dispatch("rerun_browser_check", {"tool_call_id":"original-0"}, ctx).ok)

    def test_browser_replay_requires_original_tool_workspace_and_unmodified_arguments(self):
        self.observe("run_browser", "AssertionError", args={"code":"assert page.title() == 'Ready'"},
                     meta={"exit_code":1}, ok=False)
        readonly = build_default_registry(read_only=True)
        self.p.register_browser_replay(readonly)
        self.assertNotIn("rerun_browser_check", readonly.names())

        registry = build_default_registry()
        self.p.register_browser_replay(registry)
        wrong = ToolContext(Sandbox(self.root / "another-member"), Limits())
        self.assertFalse(registry.dispatch("rerun_browser_check", {"tool_call_id":"one"}, wrong).ok)
        ctx = ToolContext(Sandbox(self.root), Limits())
        for field, value in [("cwd","another-member"), ("timeout",600), ("code","assert True")]:
            result = registry.dispatch("rerun_browser_check", {"tool_call_id":"one", field:value}, ctx)
            self.assertFalse(result.ok)
            self.assertIn("未知参数", result.content)
        self.assertTrue(self.p.blocked_reason())

    _custom_script = """import sys
failures = []
def expect(caption, got, want):
    if got == want:
        print("PASS " + caption)
    else:
        print("FAIL " + caption, got, want)
        failures.append(caption)
actual = [45, 60, 45]
expect("busy 180", sum(actual), 180)
expect("all intervals", len(actual), 3)
sys.exit(bool(failures))
"""

    def run_custom(self, source, *, baseline=False, command="python3 test_member.py",
                   output=None, truncated=False):
        import subprocess
        import sys
        path = self.root / "test_member.py"
        write = "edit_file" if path.exists() else "write_file"
        if source is not None:
            path.write_text(source)
            if baseline:
                self.p = MemberProgress(self.root)
            self.observe(write, args={"path": path.name})
        result = subprocess.run([sys.executable, str(path)], capture_output=True, text=True, check=False)
        if output is None:
            output = result.stdout
        if "; echo" in command:
            output += f"EXIT={result.returncode}\n"
        status = 0 if "; echo" in command else result.returncode
        args = {"command": command}
        state = RAOState(goal="", task_id="t", history=[{"role": "assistant", "tool_calls": [
            {"id": "one", "function": {"name": "run_shell", "arguments": json.dumps(args)}}]}])
        obs = Observation(step_id="s", source="run_shell", ok=status == 0, content="exit_code=%s\nstdout:\n%s" % (status, output),
                          truncated=truncated, meta={"exit_code": status}, tool_call_id="one")
        self.p.observe(StepRecord(step=1, observations=[obs], prompt_tokens=1), state)

    def test_complete_revised_member_script_retires_old_version_for_qa(self):
        direct = 'python3 test_member.py; echo "EXIT=$?"'
        self.run_custom(self._custom_script, command=direct)
        old = self.p.failed_checks()
        self.assertEqual(len(old), 1)
        self.run_custom(self._custom_script.replace("180", "150"), command=direct)
        self.assertIsNone(self.p.blocked_reason())
        retired, = self.p.evidence()["superseded_member_checks"]
        self.assertEqual(retired["prior_failure"], next(iter(old.values())))
        self.assertEqual(retired["current_caption"], "busy 150")
        self.assertNotEqual(retired["prior_sha256"], retired["current_sha256"])
        self.assertIn("pending_independent_acceptance", retired["disposition"])

    def test_named_failures_disclose_the_original_script_for_each_actual_case_scope(self):
        import hashlib
        source = self._custom_script.replace('len(actual), 3', 'len(actual), 4')
        self.run_custom(source)
        failures = self.p.failed_checks()
        probes = self.p.inline_failures()
        self.assertEqual(len(failures), 2)
        self.assertEqual(set(probes), set(failures))
        for scope, probe in probes.items():
            self.assertEqual(probe['case_label'], failures[scope])
            self.assertEqual(probe['code'], source)
            self.assertEqual(probe['sha256'], hashlib.sha256(source.encode()).hexdigest())
            self.assertEqual(probe['path'], 'test_member.py')
            self.assertEqual(probe['exit_code'], 1)
            self.assertEqual(probe['tool_call_id'], 'one')
            self.assertIn('FAIL busy 180', probe['error'])
            self.assertIn('FAIL all intervals', probe['error'])
        # A different script with identical case captions cannot clear them.
        other = self.root / 'another.py'
        other.write_text(source.replace('45, 60, 45', '60, 60, 60, 0'))
        self.observe('write_file', args={'path':other.name})
        self.observe('run_shell', 'exit_code=0\nstdout:\nPASS busy 180\nPASS all intervals',
                     args={'command':'python3 another.py'}, meta={'exit_code':0})
        self.assertEqual(self.p.failed_checks(), failures)
        self.assertEqual(self.p.inline_failures(), probes)

    def test_named_probe_still_requires_observed_member_edit_and_complete_failure(self):
        self.run_custom(self._custom_script, baseline=True)
        self.assertTrue(self.p.failed_checks())
        self.assertFalse(self.p.inline_failures())
        changed = self._custom_script.replace('actual =', '# member edit\nactual =')
        self.run_custom(changed, truncated=True)
        self.assertFalse(self.p.inline_failures())
        self.run_custom(None)
        probe, = self.p.inline_failures().values()
        self.assertEqual(probe['code'], changed)
        self.assertTrue(self.p.failed_checks())

    def test_preexisting_script_cannot_retire_or_mask_an_old_check(self):
        self.run_custom(self._custom_script, baseline=True)
        fixed = self._custom_script.replace("180", "150")
        self.run_custom(fixed)
        self.assertTrue(self.p.blocked_reason())
        self.run_custom('print("PASS busy 180 150 180")')
        self.assertTrue(self.p.blocked_reason())
        self.assertFalse(self.p.evidence()["superseded_member_checks"])

    def test_member_revisions_preserve_assertions_reporter_and_observed_expressions(self):
        fixed = self._custom_script.replace("180", "150")
        for changed in (fixed.replace('expect("all intervals", len(actual), 3)', ''),
                        fixed.replace('if got == want:', 'if True:'),
                        fixed.replace('sum(actual), 150', '150, 150'),
                        'print("PASS busy 180 150 180")\nprint("PASS all intervals")',
                        fixed.replace('sys.exit(bool(failures))', 'sys.exit(0)')):
            with self.subTest(changed=changed[-80:]):
                path = self.root / "test_member.py"
                path.unlink(missing_ok=True)
                self.p = MemberProgress(self.root)
                self.run_custom(self._custom_script)
                self.run_custom(changed)
                self.assertTrue(self.p.blocked_reason())
                self.assertFalse(self.p.evidence()["superseded_member_checks"])

    def test_retirement_requires_full_output_order_and_an_unfiltered_runner(self):
        self.run_custom(self._custom_script)
        fixed = self._custom_script.replace("180", "150")
        self.run_custom(fixed, command="python3 test_member.py | tail -2")
        self.run_custom(None, command="python3 test_member.py selected_case")
        self.run_custom(None, output="PASS busy 150\n")
        self.run_custom(None, output="PASS all intervals\nPASS busy 150\n")
        self.run_custom(None, truncated=True)
        self.assertTrue(self.p.blocked_reason())
        self.run_custom(None)
        self.assertIsNone(self.p.blocked_reason())

    def test_changed_member_script_invalidates_previous_revision_receipt(self):
        self.run_custom(self._custom_script)
        corrected = self._custom_script.replace("180", "150")
        self.run_custom(corrected)
        self.run_custom('print("PASS busy 180 150 180")')
        self.assertTrue(self.p.blocked_reason())
        self.assertIn("invalidated_at_step", self.p.evidence()["superseded_member_checks"][0])
        self.run_custom(corrected)
        self.assertIsNone(self.p.blocked_reason())

    def test_same_version_clears_dynamic_failure_detail_only_after_full_run(self):
        data = self.root / "minutes"
        data.write_text("45")
        source = self._custom_script.replace("actual = [45, 60, 45]",
            "from pathlib import Path\nactual = [45, 60, int(Path(__file__).with_name('minutes').read_text())]")
        self.run_custom(source)
        self.assertTrue(self.p.blocked_reason())
        data.write_text("75")
        self.run_custom(None)
        self.assertIsNone(self.p.blocked_reason())
        self.assertEqual(self.p.evidence()["superseded_member_checks"], [])

    def test_unrelated_passing_case_sharing_a_word_prefix_cannot_retire_a_failure(self):
        self.run_custom(self._custom_script)
        fixed = self._custom_script.replace("180", "150")
        self.run_custom(fixed, output="PASS busy 150 records  | 150\nPASS all intervals\n")
        self.assertTrue(self.p.blocked_reason())
        self.assertFalse(self.p.evidence()["superseded_member_checks"])

    def test_recovered_fixed_checks_remain_bound_to_original_script_bytes(self):
        original = self._custom_script
        self.run_custom(original)
        failed = self.p.failed_checks()
        self.p = MemberProgress(self.root)
        self.p.restore_failed_checks(failed)
        self.run_custom('print("PASS busy 180")')
        self.assertTrue(self.p.blocked_reason())
        self.assertEqual(self.p.failed_checks(), failed)

    def test_renamed_text_corpus_cases_require_the_original_reporter(self):
        corpus = """import sys
errors = []
def record(title, observed, target):
    if observed == target:
        print("PASS " + title)
    else:
        print("FAIL " + title)
        errors.append(title)
words = "elm elm ash".split()
record("two elm", words.count("elm"), 2)
record("three ash", words.count("ash"), 3)
sys.exit(bool(errors))
"""
        self.run_custom(corpus)
        self.assertTrue(self.p.blocked_reason())
        corrected = corpus.replace('"elm elm ash".split()', '["fir", "ash", "fir", "ash"]')
        corrected = corrected.replace('"two elm", words.count("elm")', '"fir count", words.count("fir")')
        corrected = corrected.replace('"three ash", words.count("ash"), 3', '"ash count", words.count("ash"), 2')
        self.run_custom(corrected.replace("if observed == target:", "if True:"))
        self.assertTrue(self.p.blocked_reason())
        self.run_custom(corrected)
        self.assertIsNone(self.p.blocked_reason())
        row, = self.p.evidence()["superseded_member_checks"]
        self.assertEqual((row["prior_exit_code"], row["current_exit_code"], row["observed_case_count"]), (1, 0, 2))
        self.assertEqual(row["current_caption"], "ash count")

    def test_custom_summary_distinguishes_empty_inventory_from_a_failed_case(self):
        # The QA shell displayed the footer with tail, then counted PASS rows and
        # displayed the same inventory again with grep.
        command = {"command": "python3 qa_verify.py"}
        for inventory in ("明细：无", "details: []"):
            self.observe("run_shell", f"exit_code=0\nstdout:\nPASS C4 adjacent slots\n"
                         f"==== 汇总：PASS 53 / 53，FAIL 0 ====\nFAIL {inventory}\n53\nFAIL {inventory}\n",
                         args=command, meta={"exit_code": 0})
            self.assertIsNone(self.p.blocked_reason())
        self.assertIn("1 项 PASS", self.p.evidence()["check_signals"][0])
        # A total alone never supplies a case result, even when presented by a
        # completely different wrapper.
        self.observe("run_shell", "PASS 53/53, FAIL 0\nFAIL list: none",
                     args={"command": "python3 other_qa.py"}, meta={"exit_code": 0})
        self.assertEqual(len(self.p.evidence()["check_signals"]), 1)

    def test_unbalanced_or_contradictory_custom_results_remain_failed(self):
        for body in ("FAIL details: []", "PASS 8/10, FAIL 2\nFAIL details: none",
                     "PASS actual case\nPASS 8/10, FAIL 2",
                     "PASS 2/2, FAIL 0\nFAIL reviewer assigned: none",
                     "FAIL actual case\nPASS 8/8, FAIL 0\nFAIL details: none",
                     "PASS 8/8, FAIL 0\nFAIL details: none\nEXIT=3"):
            with self.subTest(body=body):
                self.p = MemberProgress(self.root)
                self.observe("run_shell", body, args={"command": "python3 test_status.py"}, meta={"exit_code": 0})
                self.assertTrue(self.p.blocked_reason())

    def test_new_qa_runner_with_the_same_case_ids_cannot_reinstate_old_checks(self):
        old, new = "python3 independent/first.py", "python3 independent/rebuilt.py"
        self.observe("run_shell", "FAIL C4 boundary condition",
                     args={"command": old}, meta={"exit_code": 1}, ok=False)
        self.observe("run_shell", "PASS C4 boundary condition\nPASS 1/1, FAIL 0\nFAIL details: none",
                     args={"command": new}, meta={"exit_code": 0})
        self.assertTrue(self.p.blocked_reason())
        self.assertFalse(self.p.evidence()["superseded_member_checks"])
        self.observe("run_shell", "PASS C4 boundary condition", args={"command": old}, meta={"exit_code": 0})
        self.assertIsNone(self.p.blocked_reason())

    def test_incomplete_or_empty_tally_does_not_certify_the_script(self):
        for total in ("PASS 2/3, FAIL 0", "PASS 0/0, FAIL 0"):
            with self.subTest(total=total):
                self.observe("run_shell", "PASS A real case\n" + total, args={"command": "python3 test_count.py"},
                             meta={"exit_code": 0})
                self.assertEqual(self.p.evidence()["check_signals"], [])

    def test_booking04_timeout_and_wrapper_observations_leave_exact_recheck(self):
        target = self.root / "test_api.py"
        target.write_text("assert 2 + 2 == 4")
        self.observe("write_file", args={"path": target.name})
        initial = {"command": 'python3 test_api.py 2>&1 | tail -60; echo "=== EXIT: ${PIPESTATUS[0]} ==="'}
        self.observe("run_shell", "exit_code=-9\n命令执行超时（20 秒），已停止。", args=initial,
                     ok=False, meta={"exit_code": -9, "timed_out": True})
        snippet = """import subprocess, sys
p = subprocess.Popen([sys.executable, "-u", "test_api.py"], stdout=subprocess.PIPE)
stdout, _ = p.communicate()
lines = stdout.decode().splitlines()
print("FAIL lines:", [line for line in lines if "FAIL" in line] or "none")
"""
        self.observe("run_python", "exit_code=0\nstdout:\nEXIT=0 in 2.0s\nFAIL lines: none\nPASS A",
                     args={"code": snippet}, meta={"exit_code": 0})
        self.assertFalse(self.p.failed_checks())
        evidence = self.p.evidence()
        self.assertEqual(len(evidence["diagnostic_checks"]), 2)
        self.assertEqual(len(evidence["pending_verification"]), 1)
        scope = evidence["pending_verification"][0]["scope"]
        self.assertTrue(self.p.blocked_reason())  # No completed check of this candidate so far.
        self.observe("run_browser", args={"code": "assert page.title() == 'Rooms'"}, meta={"exit_code": 0})
        self.assertIsNone(self.p.blocked_reason())  # Candidate may be seen by another member.
        for args in ({"command": "python3 test_api.py selected"}, {"command": "python3 test_api.py | tail -1"}):
            self.observe("run_shell", "PASS A", args=args, meta={"exit_code": 0})
            self.assertTrue(self.p.evidence()["pending_verification"])
        self.observe("run_shell", "PASS A", args={"command": "python3 test_api.py"}, meta={"exit_code": 0})
        self.assertFalse(self.p.evidence()["pending_verification"])
        self.assertIn(scope, self.p.evidence()["complete_verification"])

    def test_mixed_shell_cannot_invent_a_blocker_or_a_green_suite(self):
        for text in ("FAIL strange diagnostic in any language", "PASS A\nPASS B", "3 passed in 0.1s"):
            self.observe("run_shell", text, args={"command": "python3 app.py; echo 'rendered report'"},
                         meta={"exit_code": 0})
        self.assertFalse(self.p.failed_checks())
        self.assertFalse(self.p.evidence()["check_signals"])
        self.assertTrue(self.p.evidence()["diagnostic_checks"])

    def test_default_member_python_can_recheck_a_stored_host_interpreter_scope(self):
        target = self.root / "test_cases.py"
        target.write_text("assert 2 + 2 == 4\nprint('PASS addition')\n")
        self.observe("write_file", args={"path": target.name})
        old_scope = hashlib.sha256("\0".join((".", sys.executable, target.name)).encode()).hexdigest()
        self.p.restore_pending_verification([{
            "scope": old_scope, "check": shlex.join([sys.executable, target.name]), "path": target.name,
            "at_sha256": hashlib.sha256(target.read_bytes()).hexdigest()}])
        self.assertEqual(self.p.evidence()["pending_verification"][0]["check"], "python test_cases.py")
        self.observe("run_shell", "PASS addition", args={"command":"python test_cases.py subset"},
                     meta={"exit_code": 0})
        self.assertTrue(self.p.evidence()["pending_verification"])
        self.observe("run_shell", "PASS addition", args={"command":"python test_cases.py"}, meta={"exit_code": 0})
        self.assertFalse(self.p.evidence()["pending_verification"])
        self.assertIn(old_scope, self.p.evidence()["complete_verification"])

    def test_poll_only_uses_terminal_host_receipt_for_its_original_command(self):
        command = "python3 test_inventory.py"
        self.observe("run_shell", "still running", args={"command": command}, meta={"process_state":"running"})
        self.observe("poll_process", "PASS synthetic progress", meta={"process_state":"running"})
        self.assertEqual(self.p.evidence()["executions"]["run_shell_calls"], 0)
        self.observe("poll_process", "FAIL inventory", args={"command":"python3 irrelevant.py"},
                     ok=False, meta={"process_state":"exited","command":command,"cwd":".","exit_code":1})
        self.assertTrue(self.p.failed_checks())
        self.observe("run_shell", "PASS inventory", args={"command":command}, meta={"exit_code":0})
        self.assertFalse(self.p.failed_checks())
        self.assertEqual(self.p.evidence()["executions"]["run_shell_calls"], 2)

    def test_real_truncation_metadata_is_distinct_from_business_text(self):
        self.observe("run_shell", "PASS 截断的 JSON 可以读取", args={"command":"python3 test_formats.py"},
                     meta={"exit_code":0, "truncated":False})
        self.assertIn("1 项 PASS", self.p.evidence()["check_signals"][0])
        src = self.root / "test_formats.py"
        src.write_text("assert len('xy') == 2")
        self.observe("write_file", args={"path":src.name})
        command = {"command":"python3 test_formats.py"}
        self.observe("run_shell", "FAIL named case", args=command, meta={"exit_code":1}, ok=False)
        self.observe("run_shell", "PASS another case\n…[已截断]…\nPASS last", args=command,
                     meta={"exit_code":0, "truncated":True})
        self.assertTrue(self.p.failed_checks())
        self.assertIn("输出已截断", self.p.evidence()["check_signals"][-1])

    def test_clipped_print_only_script_cannot_confirm_a_pending_verification(self):
        path = self.root / "dummy.py"
        path.write_text("print('PASS ' * 9000)")
        self.observe("write_file", args={"path":path.name})
        self.observe("run_shell", "PASS yes", args={"command":"python3 dummy.py"},
                     meta={"exit_code":0,"truncated":True})
        self.assertFalse(self.p.evidence()["complete_verification"])
        self.observe("run_shell", "PASS yes", args={"command":"python3 dummy.py"}, meta={"exit_code":0})
        self.assertFalse(self.p.evidence()["check_signals"])

    def test_quiet_script_new_revision_can_reach_peer_but_its_exit_is_not_a_verdict(self):
        target = self.root / "test_inventory.py"
        target.write_text("raise ValueError('wrong value')\n")
        self.observe("write_file", args={"path": target.name})
        cmd = "python3 test_inventory.py"
        registry = build_default_registry()
        ctx = ToolContext(Sandbox(self.root), Limits())
        def run(command):
            args = {"command": command}
            result = registry.dispatch("run_shell", args, ctx)
            self.observe("run_shell", result.content, ok=result.ok, args=args, meta=result.meta)
            return result
        self.assertTrue(run(cmd + " 2>&1 | tail -80").ok)
        [pending] = self.p.evidence()["pending_verification"]
        self.assertTrue(self.p.blocked_reason())

        target.write_text("from pathlib import Path\n"
                          "remaining = len(Path('module.py').read_text())\n"
                          "print('[1] inventaire terminé')\n"
                          "raise SystemExit(0 if remaining else 1)\n")
        self.observe("edit_file", args={"path": target.name})
        for indirect in [cmd + " subset", cmd + " | tail -1", cmd + "; echo ok"]:
            self.assertTrue(run(indirect).ok)
            self.assertTrue(self.p.blocked_reason())
        self.assertTrue(run(cmd).ok)
        evidence = self.p.evidence()
        self.assertIsNone(self.p.blocked_reason())
        self.assertFalse(evidence["check_signals"])
        self.assertFalse(evidence["complete_verification"])
        self.assertFalse(self.p.review_evidence())
        [subject] = evidence["pending_verification"]
        self.assertNotEqual(subject["current_sha256"], pending["at_sha256"])
        receipt = evidence["script_exits"][subject["scope"]]
        self.assertEqual(receipt["sha256"], subject["current_sha256"])
        self.assertEqual(receipt["exit_code"], 0)
        self.assertIn("独立", self.p.steering())
        self.src.write_text("a different candidate version")
        self.observe("edit_file", args={"path": self.src.name})
        self.assertFalse(self.p.evidence()["script_exits"])
        self.assertTrue(self.p.blocked_reason())
        self.assertTrue(run(cmd).ok)
        self.observe("run_shell", "interrupted", args={"command": cmd}, ok=False,
                     meta={"exit_code": -9, "timed_out": True})
        self.assertFalse(self.p.evidence()["script_exits"])
        self.assertTrue(self.p.blocked_reason())

    def test_echo_wrapper_only_certifies_the_final_child_exit_receipt(self):
        path = self.root / "test_inventory.py"
        path.write_text("assert 2 == 2")
        self.observe("write_file", args={"path":path.name})
        cmd = {"command": 'python3 test_inventory.py; echo "EXIT=$?"'}
        self.observe("run_shell", "PASS inventory", args=cmd, meta={"exit_code":0,"truncated":True})
        self.assertTrue(self.p.evidence()["pending_verification"])
        self.assertFalse(self.p.evidence()["complete_verification"])
        self.observe("run_shell", "FAIL inventory\nEXIT=1", args=cmd, meta={"exit_code":0})
        self.assertTrue(self.p.failed_checks())
        self.observe("run_shell", "PASS inventory\nEXIT=0", args=cmd, meta={"exit_code":0})
        self.assertFalse(self.p.failed_checks())
        self.assertFalse(self.p.evidence()["pending_verification"])
