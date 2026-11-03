"""CLI 端到端测试：保证 `demo` 与 `eval` 在无网络、无 key 的情况下可跑。"""

from __future__ import annotations

import contextlib
import io
import unittest
from pathlib import Path

from miniagent.cli import main
from tests.support import cleanup, make_temp_dir

REPO = Path(__file__).resolve().parents[1]


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.base = make_temp_dir("cli")

    def tearDown(self) -> None:
        cleanup(self.base)

    def _run(self, argv: list[str]) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = main(argv)
        return code, buffer.getvalue()

    def test_demo_succeeds_offline(self) -> None:
        code, output = self._run(["demo", "--runs-dir", str(self.base / "runs")])
        self.assertEqual(code, 0, output)
        self.assertIn("任务完成率 : 100.0%", output)
        run_dirs = [p for p in (self.base / "runs").iterdir() if p.is_dir()]
        self.assertEqual(len(run_dirs), 1)
        self.assertTrue((run_dirs[0] / "trace.jsonl").exists())
        self.assertTrue(any((run_dirs[0] / "agents").rglob("notes.md")))

    def test_eval_with_mock_writes_report(self) -> None:
        code, output = self._run(
            [
                "eval",
                "--tasks",
                str(REPO / "examples" / "tasks.jsonl"),
                "--mock",
                str(REPO / "examples" / "mock_task.json"),
                "--runs-dir",
                str(self.base / "runs"),
            ]
        )
        self.assertEqual(code, 0, output)
        self.assertIn("(3/3)", output)
        run_dir = next(p for p in (self.base / "runs").iterdir() if p.is_dir())
        report = (run_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("任务完成率", report)
        self.assertIn("100.0%", report)

    def test_run_without_key_reports_configuration_error(self) -> None:
        import os

        saved = {key: os.environ.pop(key, None) for key in ("DEEPSEEK_API_KEY",)}
        try:
            code, output = self._run(
                ["run", "随便跑一个任务", "--runs-dir", str(self.base / "runs")]
            )
        finally:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value
        self.assertEqual(code, 2)
        self.assertIn("DEEPSEEK_API_KEY", output)

    def test_orchestrate_serial_with_mock(self) -> None:
        code, output = self._run(
            [
                "orchestrate",
                "整理笔记并写摘要",
                "--mock",
                str(REPO / "examples" / "mock_plan.json"),
                "--runs-dir",
                str(self.base / "runs"),
            ]
        )
        self.assertEqual(code, 0, output)
        self.assertIn("子任务完成率", output)
        run_dir = next(p for p in (self.base / "runs").iterdir() if p.is_dir())
        self.assertTrue((run_dir / "plan.json").exists())
        self.assertTrue((run_dir / "board.json").exists())
        report = (run_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("子任务", report)
        self.assertIn("总体目标", report)

    def test_orchestrate_parallel_with_mock_is_rejected(self) -> None:
        code, output = self._run(
            [
                "orchestrate",
                "随便",
                "--mock",
                str(REPO / "examples" / "mock_plan.json"),
                "--scheduler",
                "parallel",
                "--runs-dir",
                str(self.base / "runs"),
            ]
        )
        self.assertEqual(code, 2)
        self.assertIn("不是线程安全", output)

    def test_report_command(self) -> None:
        self._run(["demo", "--runs-dir", str(self.base / "runs")])
        run_dir = next(p for p in (self.base / "runs").iterdir() if p.is_dir())
        code, output = self._run(["report", str(run_dir)])
        self.assertEqual(code, 0)
        self.assertIn("逐任务明细", output)


if __name__ == "__main__":
    unittest.main()
