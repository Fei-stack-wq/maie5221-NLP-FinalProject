"""Crew scheduling receipts control reinspection and never certify acceptance."""
import json
import tempfile
import unittest
from pathlib import Path

from miniagent.config import Limits
from miniagent.crew_progress import MemberProgress
from miniagent.rao import Budget, Observation, RAOState, StepRecord
from miniagent.sandbox import Sandbox
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

    def observe(self, tool="run_python", text="", *, args=None, ok=True, meta=None):
        message = {"role": "assistant", "tool_calls": [{"id": "one", "function": {
            "name": tool, "arguments": json.dumps(args or {})}}]}
        state = RAOState(goal="", task_id="t", history=[message])
        obs = Observation(step_id="s", source=tool, ok=ok, content=text, meta=meta or {}, tool_call_id="one")
        self.p.observe(StepRecord(step=1, observations=[obs], prompt_tokens=500), state)

    def check(self, text, command="unittest discover", *, duration="1.123"):
        args = {"code": f"r = subprocess.run({command.split()!r}); print(r.stdout)"}
        self.observe(text="exit_code=0\nstdout:\n" + text.replace("$", duration), args=args)

    def test_same_pass_with_new_timing_and_print_format_closes(self):
        self.src.write_text("final")
        self.check("Ran 34 tests in $s\n\nOK")
        self.assertEqual(self.p.phase(self.budget), "work")
        self.check("0 0\n['Ran 34 tests in $s', 'OK']", duration="4.015")
        self.assertEqual(self.p.phase(self.budget), "handoff")
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
        self.assertEqual(self.p.phase(self.budget), "handoff")

    def test_missing_required_file_is_not_inferred_from_test_output(self):
        self.p = MemberProgress(self.root, ["report.md"])
        for i in range(8):
            self.check("5 passed in 0.1s", "pytest")
        self.assertEqual(self.p.phase(self.budget), "handoff")
        self.assertIn("report.md", self.p.blocked_reason())
        self.assertIn("[BLOCKED]", self.p.steering())

    def test_pagination_can_progress_but_repeated_inspection_closes(self):
        for i in range(10):
            self.observe("read_file", meta={"path": "module.py", "offset": 100 * i})
            self.assertEqual(self.p.phase(self.budget), "work")
        for i in range(7):
            self.observe(args={"code": f"print(open('module.py').read()[{i}:])"})
        self.assertEqual(self.p.phase(self.budget), "handoff")
        self.assertIsNone(self.p.blocked_reason())
        self.assertEqual(self.p.evidence()["inspected"], ["module.py"])

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

    def test_dry_listing_preserves_blocker_and_verbal_review_can_finish(self):
        self.assertIsNone(self.p.blocked_reason())
        for i in range(7):
            self.observe("list_dir")
        self.assertEqual(self.p.phase(self.budget), "handoff")
        self.assertTrue(self.p.blocked_reason())

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
        self.assertEqual(self.p.phase(self.budget), "handoff")
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

    def test_exact_repetition_requests_handoff_before_rao_loop_limit(self):
        observation = Observation(step_id="s", source="list_dir", ok=True, content="")
        state = RAOState(goal="", task_id="t", scratch={"repeat": {"count": 4}})
        self.p.observe(StepRecord(step=4, observations=[observation]), state)
        self.assertEqual(self.p.phase(self.budget), "handoff")
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
