"""指标测试：完成率与耗时的计算口径必须固定，否则报告数字对不上。"""

from __future__ import annotations

import unittest
from pathlib import Path

from miniagent.metrics import Tracer, load_events, render_report, summarize
from miniagent.contracts.acceptance import AcceptanceReport, AcceptanceSpec, validate_report
from tests.support import cleanup, make_temp_dir


def acceptance_fields(task_id: str, attempt_id: str = "a1", verdict: str = "passed") -> dict:
    """A real contract report from a trusted deterministic test evaluator."""
    at = "2026-11-16T10:00:00Z"
    spec = AcceptanceSpec(
        created_at=at, spec_id=f"spec-{task_id}", version=1, task_id=task_id,
        objective="Verify the fixture result", target={"project_id": "fixture-project", "environment_id": "fixture-env", "resource_ids": []},
        criteria=[{"id": "C1", "description": "Fixture value matches", "required": True}],
        constraints_ref={"id": "fixture-constraints", "version": 1},
        evaluator_ref={"id": "fixture-check", "version": 1, "kind": "deterministic"},
        params={}, required_output_roles=[], authority_ref="fixture-authority",
    )
    evidence = {"kind": "receipt", "id": f"receipt-{task_id}-{attempt_id}"}
    report = AcceptanceReport(
        created_at=at, report_id=f"report-{task_id}-{attempt_id}", task_id=task_id, attempt_id=attempt_id,
        spec_ref={"id": spec.spec_id, "version": spec.version}, evaluator_ref=spec.evaluator_ref,
        verdict=verdict,
        checks=[{"criterion_id": "C1", "verdict": verdict, "reason": "Fixture comparison", "evidence_refs": [evidence]}],
        evaluated_artifact_refs=[], evaluated_resource_versions=[], evidence_refs=[evidence],
        reason=None if verdict == "passed" else "Fixture comparison failed", evaluated_at=at,
    )
    validate_report(spec, report)
    receipt = {
        "id": evidence["id"], "kind": "acceptance_invocation", "runtime_local": True,
        "task_id": task_id, "attempt_id": attempt_id, "environment_id": spec.target.environment_id,
        "started_at": at, "ended_at": at, "invoked": True, "outcome": "returned",
        "results": {"C1": [verdict == "passed", "Fixture comparison"]},
        "source": "synthetic test fixture; no external task was executed",
    }
    return {"acceptance_spec": spec.model_dump(mode="json"), "acceptance_report": report.model_dump(mode="json"),
            "acceptance_evidence": [receipt]}


def trace_event(kind: str, task_id: str, *, root_id: str | None = None, attempt_id: str = "a1", **payload) -> dict:
    root_id = root_id or task_id
    return {
        "schema_version": 2, "kind": kind, "root_id": root_id, "task_id": task_id,
        "attempt_id": attempt_id, "level": "goal" if root_id == task_id else "subtask", **payload,
    }


class MetricsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.run_dir = make_temp_dir("metrics")

    def tearDown(self) -> None:
        cleanup(self.run_dir)

    def _write_fixture(self) -> None:
        with Tracer(self.run_dir) as tracer:
            tracer.event("case_registered", root_id="t1", task_id="t1", level="goal")
            tracer.event("case_registered", root_id="t2", task_id="t2", level="goal")
            tracer.event("task_start", task_id="t1", root_id="t1", attempt_id="a1", level="goal", agent="a", task="任务一")
            tracer.event("step", task_id="t1", root_id="t1", attempt_id="a1", level="goal", step=1, prompt_tokens=10, completion_tokens=5)
            tracer.event(
                "task_end",
                task_id="t1",
                root_id="t1", attempt_id="a1", level="goal",
                agent="a",
                status="completed",
                duration_ms=100.0,
                steps=1,
                tool_calls=0,
                prompt_tokens=10,
                completion_tokens=5,
                error="",
                **acceptance_fields("t1"),
            )
            tracer.event("task_end", task_id="t2", root_id="t2", attempt_id="a1", level="goal", agent="a", status="limit_exceeded", duration_ms=300.0, steps=3, tool_calls=2, error="超过最大步数 12")

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
