"""Display truncation must not discard a bounded original failure receipt."""
import os
import shlex
import tempfile
import unittest
from pathlib import Path

from miniagent.config import Limits
from miniagent.sandbox import Sandbox
from miniagent.tools import ToolContext, build_default_registry


@unittest.skipUnless(os.name == "posix", "requires the POSIX shell runner")
class BoundedFailureOutputTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.ctx = ToolContext(sandbox=Sandbox(Path(folder.name).resolve()),
                               limits=Limits(tool_timeout_seconds=2))
        self.registry = build_default_registry()

    def run_code(self, code):
        return self.registry.dispatch("run_shell", {"command":"python3 -c " + shlex.quote(code)}, self.ctx)

    def test_complete_failure_is_separate_from_unchanged_truncated_display(self):
        result = self.run_code("print('A'*3000); print('FAIL exact case'); print('B'*3000); raise SystemExit(1)")
        self.assertFalse(result.ok)
        self.assertTrue(result.meta["truncated"])
        self.assertNotIn("FAIL exact case", result.content)
        complete = result.meta["complete_failure_output"]
        self.assertIn("A" * 3000, complete)
        self.assertIn("FAIL exact case", complete)
        self.assertIn("B" * 3000, complete)
        self.assertLess(len(complete.encode()), 8192)
        self.assertLess(len(result.content.encode()), 4000)
        self.assertEqual(result.meta["exit_code"], 1)

    def test_larger_failure_and_success_do_not_receive_failure_archive(self):
        for code in ("print('X'*9000); raise SystemExit(1)", "print('X'*6000)"):
            with self.subTest(code=code):
                result = self.run_code(code)
                self.assertTrue(result.meta["truncated"])
                self.assertNotIn("complete_failure_output", result.meta)

    def test_inline_python_keeps_complete_error_for_a_peer_with_unchanged_display_size(self):
        for size, retained in ((9000, True), (34000, False)):
            with self.subTest(size=size):
                result = self.registry.dispatch("run_python", {
                    "code":f"import sys\nprint('X'*{size}, file=sys.stderr)\nassert False, 'receipt marker'"
                }, self.ctx)
                self.assertTrue(result.meta["truncated"])
                self.assertLess(len(result.content.encode()), 4000)
                self.assertEqual("complete_failure_output" in result.meta, retained)
                if retained:
                    self.assertIn("X"*size, result.meta["complete_failure_output"])

    def test_interrupted_output_is_not_complete_failure_evidence(self):
        self.ctx.limits.tool_timeout_seconds = 0.15
        result = self.run_code("import time; print('X'*6000, flush=True); time.sleep(4)")
        self.assertFalse(result.ok)
        self.assertTrue(result.meta["timed_out"])
        self.assertNotIn("complete_failure_output", result.meta)


if __name__ == "__main__":
    unittest.main()
