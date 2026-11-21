import hashlib
import tempfile
import unittest
from pathlib import Path

from miniagent.rao import Action, Budget, Observation, RAOState, StepPolicy, policy_messages
from miniagent.tool_progress import ToolProgress
from tests.test_rao import _unit


def command(name, **args):
    return {"tool_calls": [{"name": name, "arguments": args}]}


class ToolProgressTest(unittest.TestCase):
    def test_seven_step_cycle_reports_named_failure_unchanged_hash_and_missing_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = "logic.test.js"
            (root / path).write_text("rewind\n")
            probe = command("run_python", code="print('snapshot checked')")
            seven = [probe] * 4 + [
                command("read_lines", path=path),
                command("edit_file", path=path, old_text="rewind", new_text="rewind"),
                command("run_shell", command="printf '  ✗ 场地恢复到消行前\\n结果: 51 通过, 1 失败\\n'; exit 1"),
            ]
            script = seven * 5 + [command("write_file", path="README.md", content="controls"), *seven]
            unit = _unit(root, script)
            unit.missing_deliverables = lambda: [] if (root / "README.md").is_file() else ["README.md"]
            state = RAOState(goal="finish", task_id="one", history=[{"role": "user", "content": "finish"}])
            notices = []
            for step in range(len(script)):
                result = unit.step(state, Budget())
                self.assertIsNone(result.terminate_reason)
                if result.progress_advisory:
                    notices.append((step + 1, result.progress_advisory))
                    self.assertEqual(state.history[-1]["content"], result.progress_advisory)
                    self.assertEqual(state.history[-2]["role"], "tool")
                    self.assertIn(result.progress_advisory,
                                  [row["content"] for row in policy_messages(state, StepPolicy(phase="work", compact=True))])
            self.assertEqual([step for step, notice in notices], [21])
            note = notices[0][1]
            digest = hashlib.sha256(b"rewind\n").hexdigest()
            for part in ("场地恢复到消行前", "51 通过, 1 失败", path, digest[:12], "README.md",
                         "run_python × 4", "read_lines", "edit_file", "run_shell"):
                self.assertIn(part, note)
            self.assertTrue((root / "README.md").is_file())
            self.assertNotIn(note, [row["content"] for row in policy_messages(state, StepPolicy(phase="work", compact=True))])

    def test_matching_suite_pass_starts_fresh_and_failure_excerpt_survives_a_closed_workspace(self):
        tracker = ToolProgress()
        action = Action(id="tool", tool="run_shell", arguments={"command": "pytest -q"})
        def result(text, code):
            observation = Observation(step_id="s", action_id="a", source="run_shell", content=text,
                                      ok=code == 0, meta={"exit_code": code})
            return tracker.observe_batch([action], [observation], 3, failed_probe)
        def failed_probe():
            raise FileNotFoundError()

        for case in ("wrong.py::before", "correct.py::after", "correct.py::after"):
            self.assertIsNone(result("FAILED " + case + "\n1 failed", 1))
        notice = result("FAILED correct.py::after\n1 failed", 1)
        self.assertIn("correct.py::after", notice)
        self.assertIn("未能读取", notice)
        self.assertIsNone(result("1 passed", 0))
        self.assertFalse(tracker.current_advisory)
        for _ in range(2):
            self.assertIsNone(result("FAILED correct.py::after", 1))

    def test_failure_in_the_middle_of_a_batch_followed_by_real_write_is_not_a_stall(self):
        tracker = ToolProgress()
        check = Action(id="test", tool="run_python", arguments={"code": "verify()"})
        fail = Observation(step_id="s", action_id="a", source="run_python", ok=False,
                           content="exit_code=1\nstderr:\nmissing fixture", meta={"exit_code": 1})
        read = Action(id="r", tool="read_file", arguments={"path": "fixture"})
        version = Observation(step_id="s", action_id="r", source="read_file", ok=True,
                              content="a", meta={"path": "fixture", "sha256": "first"})
        tracker.observe_batch([read], [version], 3, None)
        for _ in range(2):
            self.assertIsNone(tracker.observe_batch([check], [fail], 3, None))
        write = Action(id="write", tool="edit_file", arguments={"path": "fixture"})
        changed = Observation(step_id="s", action_id="w", source="edit_file", ok=True,
                              content="changed", meta={"changed": True, "path": "fixture", "sha256": "second"})
        self.assertIsNone(tracker.observe_batch([check, write], [fail, changed], 3, None))
        self.assertIsNone(tracker.observe_batch([check], [fail], 3, None))

    def test_writing_unrelated_files_and_restoring_a_checked_file_preserve_recurrence(self):
        tracker = ToolProgress()
        def record(tool, args, success, meta, content=""):
            action = Action(tool=tool, arguments=args)
            obs = Observation(source=tool, step_id="s", action_id="a", ok=success, meta=meta, content=content)
            return tracker.observe_batch([action], [obs], 3, None)
        def write(path, digest):
            return record("write_file", {"path": path}, True, {"path": path, "sha256": digest, "changed": True})
        def rerun():
            return record("run_shell", {"command": "node test.js"}, False, {"exit_code": 1}, "not ok 2 - restore position")
        write("test.js", "v1")
        self.assertIsNone(rerun())
        for count in (1, 2):
            write("notes", f"diagnostic {count}")
            write("test.js", "tmp")
            write("test.js", "v1")
            notice = rerun()
        self.assertIn("restore position", notice)
        write("test.js", "new fixture")
        self.assertIsNone(rerun())
        self.assertIsNone(rerun())

    def test_repeated_successful_check_requires_evidence_of_unchanged_edits(self):
        tracker = ToolProgress()
        action = Action(tool="run_shell", arguments={"command": "pytest -q"})
        passed = Observation(step_id="s", source="run_shell", action_id="a", ok=True, content="5 passed in 0.2s",
                             meta={"exit_code": 0})
        for _ in range(4):
            self.assertIsNone(tracker.observe_batch([action], [passed], 3, None))
        edit = Action(tool="edit_file", arguments={"path": "subject"})
        noop = Observation(step_id="s", source="edit_file", action_id="e", ok=False, content="no_change",
                           meta={"path": "subject", "changed": False, "sha256": "v1"})
        self.assertIsNone(tracker.observe_batch([edit], [noop], 3, None))
        self.assertIn("5 passed", tracker.observe_batch([action], [passed], 3, lambda: ["notes"]))
