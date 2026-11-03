"""指标测试：完成率与耗时的计算口径必须固定，否则报告数字对不上。"""

from __future__ import annotations

import unittest
from pathlib import Path

from miniagent.metrics import Tracer, load_events, render_report, summarize
from tests.support import cleanup, make_temp_dir


class MetricsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.run_dir = make_temp_dir("metrics")

    def tearDown(self) -> None:
        cleanup(self.run_dir)

    def _write_fixture(self) -> None:
        with Tracer(self.run_dir) as tracer:
            tracer.event("task_start", task_id="t1", agent="a", task="任务一")
            tracer.event("step", task_id="t1", step=1, prompt_tokens=10, completion_tokens=5)
            tracer.event(
                "task_end",
                task_id="t1",
                agent="a",
                status="completed",
                duration_ms=100.0,
                steps=1,
                tool_calls=0,
                prompt_tokens=10,
                completion_tokens=5,
                error="",
            )
            tracer.event("task_end", task_id="t2", agent="a", status="limit_exceeded", duration_ms=300.0, steps=3, tool_calls=2, error="超过最大步数 12")

    def test_completion_rate_and_durations(self) -> None:
        self._write_fixture()
        summary = summarize(load_events(self.run_dir))
        self.assertEqual(summary["total_tasks"], 2)
        self.assertEqual(summary["completed"], 1)
        self.assertAlmostEqual(summary["completion_rate"], 0.5)
        self.assertAlmostEqual(summary["mean_duration_ms"], 200.0)
        self.assertAlmostEqual(summary["total_duration_ms"], 400.0)
        self.assertIn("超过最大步数 12", summary["failure_modes"])

    def test_tokens_counted_from_step_events(self) -> None:
        self._write_fixture()
        summary = summarize(load_events(self.run_dir))
        self.assertEqual(summary["total_tokens"], 15)

    def test_empty_run_is_safe(self) -> None:
        summary = summarize(load_events(self.run_dir))
        self.assertEqual(summary["total_tasks"], 0)
        self.assertAlmostEqual(summary["completion_rate"], 0.0)

    def test_report_file_written(self) -> None:
        self._write_fixture()
        report = render_report(self.run_dir)
        self.assertIn("任务完成率", report)
        self.assertIn("50.0%", report)
        self.assertTrue((self.run_dir / "report.md").exists())

    def test_corrupted_line_is_ignored(self) -> None:
        self._write_fixture()
        with (self.run_dir / "trace.jsonl").open("a", encoding="utf-8") as handle:
            handle.write('{"kind": "task_end", "status": "comp')  # 进程被杀留下的半行
        events = load_events(self.run_dir)
        self.assertEqual(summarize(events)["total_tasks"], 2)


if __name__ == "__main__":
    unittest.main()
