"""Runtime acceptance checks use host callbacks and observed file bytes."""
from __future__ import annotations

import hashlib
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from miniagent.acceptance import (
    AcceptanceBinding, accept_files, bind, evaluate,
    workspace_environment_id, workspace_resource_id,
)
from miniagent.contracts.acceptance import AcceptanceReport, AcceptanceSpec, validate_report
from miniagent.contracts.base import sha256_bytes
from tests.support import cleanup, make_temp_dir


class RuntimeAcceptanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = make_temp_dir("acceptance")

    def tearDown(self) -> None:
        cleanup(self.root)

    def evaluate(self, checker):
        return evaluate(checker, self.root, task_id="task-a", attempt_id="attempt-a", objective="检查最终内容")

    def spec(self, **changes) -> AcceptanceSpec:
        value = {
            "created_at": "2026-01-01T00:00:00Z", "spec_id": "spec-a", "version": 2,
            "task_id": "task-a", "objective": "内容达到固定要求",
            "target": {"project_id": "project-a", "environment_id": workspace_environment_id(self.root), "resource_ids": []},
            "criteria": [{"id": "content", "description": "内容正确", "required": True}],
            "constraints_ref": {"id": "constraints-a", "version": 1},
            "evaluator_ref": {"id": "host-checker", "version": 3, "kind": "deterministic"},
            "params": {}, "required_output_roles": [], "authority_ref": "trusted-test-caller",
        }
        value.update(changes)
        return AcceptanceSpec.model_validate(value)

    def test_no_checker_is_unverified_without_fabricated_evidence(self) -> None:
        result = self.evaluate(None)
        self.assertIsNone(result.spec)
        self.assertEqual(result.report.verdict, "unverified")
        self.assertIn("no-acceptance", result.report.reason)
        self.assertEqual(result.report.checks, ())
        self.assertEqual(result.receipts, ())
        validate_report(None, result.report)

    def test_legacy_callback_receipt_is_explicitly_runtime_local(self) -> None:
        seen = []

        def check(workspace):
            seen.append(workspace)
            return True, "由调用方检查器确认"

        result = self.evaluate(check)
        self.assertEqual(seen, [self.root])
        self.assertEqual(result.report.verdict, "passed")
        self.assertTrue(result.spec.params["runtime_local"])
        self.assertEqual(result.spec.params["evidence_scope"], "host_callback_return")
        self.assertEqual(result.report.evaluated_artifact_refs, ())
        self.assertEqual(result.report.evaluated_resource_versions, ())
        receipt = result.receipts[0]
        self.assertTrue(receipt["invoked"])
        self.assertEqual(receipt["results"]["result"], (True, "由调用方检查器确认"))
        self.assertEqual(result.report.evidence_refs[0].id, receipt["id"])

    def test_false_callback_is_failed(self) -> None:
        result = self.evaluate(lambda _: (False, "目标内容不正确"))
        self.assertEqual(result.report.verdict, "failed")
        self.assertIn("目标内容不正确", result.report.reason)

    def test_invalid_callback_results_are_errors_not_boolean_coercions(self) -> None:
        for output in (("false", "reason"), (1, "reason"), (True, 123), (True, " "),
                       [True, "reason"], True, None, (True, "reason", "extra")):
            with self.subTest(output=output):
                result = self.evaluate(lambda _, value=output: value)
                self.assertEqual(result.report.verdict, "error")
                validate_report(result.spec, result.report)

    def test_checker_exception_is_distinct_from_failed_content(self) -> None:
        def broken(_):
            raise RuntimeError("checker bug")

        result = self.evaluate(broken)
        self.assertEqual(result.report.verdict, "error")
        self.assertIn("RuntimeError: checker bug", result.report.reason)
        self.assertEqual(result.receipts[0]["outcome"], "error")

    def test_trace_payload_is_json_and_reports_roundtrip(self) -> None:
        result = self.evaluate(lambda _: (True, "verified"))
        payload = json.loads(json.dumps(result.trace_payload()))
        self.assertEqual(AcceptanceReport.model_validate(payload["report"]), result.report)
        self.assertEqual(AcceptanceSpec.model_validate(payload["spec"]), result.spec)
        with self.assertRaises(TypeError):
            result.receipts[0]["results"]["result"] = (False, "changed")
        payload["receipts"][0]["results"]["result"][0] = False
        self.assertEqual(result.receipts[0]["results"]["result"][0], True)

    def test_each_evaluation_has_its_own_report_and_invocation(self) -> None:
        checker = lambda _: (True, "verified")
        first, second = self.evaluate(checker), self.evaluate(checker)
        self.assertNotEqual(first.report.report_id, second.report.report_id)
        self.assertNotEqual(first.receipts[0]["id"], second.receipts[0]["id"])
        self.assertEqual(first.report.attempt_id, second.report.attempt_id)

    def test_bind_fixes_callback_spec_across_same_attempt_repair(self) -> None:
        decisions = iter([(False, "not ready"), (True, "repaired")])
        binding = bind(lambda _: next(decisions), self.root, task_id="task-a", objective="检查最终内容")
        first, second = self.evaluate(binding), self.evaluate(binding)
        self.assertEqual(first.report.verdict, "failed")
        self.assertEqual(second.report.verdict, "passed")
        self.assertEqual(first.report.spec_ref, second.report.spec_ref)
        self.assertEqual(first.spec, binding.spec)
        self.assertEqual(second.spec, binding.spec)
        self.assertEqual(first.report.attempt_id, second.report.attempt_id)
        self.assertNotEqual(first.report.report_id, second.report.report_id)

    def test_bind_fixes_file_spec_while_observed_content_changes(self) -> None:
        binding = bind(accept_files("fixed.txt", min_bytes=1), self.root,
                       task_id="task-a", objective="检查最终内容")
        first = self.evaluate(binding)
        (self.root / "fixed.txt").write_text("repaired\n", encoding="utf-8")
        second = self.evaluate(binding)
        self.assertEqual(first.report.verdict, "failed")
        self.assertEqual(second.report.verdict, "passed")
        self.assertEqual(first.report.spec_ref, second.report.spec_ref)
        self.assertEqual(first.spec, second.spec)
        self.assertEqual(first.report.evaluated_resource_versions[0].state, "absent")
        self.assertEqual(second.report.evaluated_resource_versions[0].state, "present")

    def test_bind_preserves_explicit_binding_and_none(self) -> None:
        original = AcceptanceBinding(self.spec(), lambda _: (True, "verified"))
        self.assertIs(bind(original, self.root, task_id="task-a", objective="ignored"), original)
        self.assertIsNone(bind(None, self.root, task_id="task-a", objective="ignored"))

    def test_real_file_content_hash_size_and_resource_version(self) -> None:
        content = "# 检查\n解释器正确，程序恢复。\n".encode("utf-8")
        (self.root / "notes.md").write_bytes(content)
        checker = accept_files("notes.md", contains={"notes.md": "解释器正确"},
                               sha256={"notes.md": hashlib.sha256(content).hexdigest()})
        self.assertEqual(checker(self.root), (True, "产物齐全"))
        result = self.evaluate(checker)
        self.assertEqual(result.report.verdict, "passed")
        receipt = result.receipts[0]
        self.assertEqual(receipt["kind"], "file_observation")
        self.assertEqual(receipt["size_bytes"], len(content))
        self.assertEqual(receipt["content_hash"], sha256_bytes(content))
        self.assertEqual(receipt["resource_id"], workspace_resource_id(self.root, "notes.md"))
        self.assertEqual(result.report.evaluated_resource_versions[0].version, sha256_bytes(content))
        self.assertEqual(result.report.evaluated_artifact_refs, ())
        self.assertEqual({ref.id for ref in result.report.evidence_refs}, {r["id"] for r in result.receipts})

    def test_file_exists_but_wrong_content_or_hash_fails(self) -> None:
        (self.root / "a.txt").write_text("actual incorrect content\n", encoding="utf-8")
        for checker in (accept_files("a.txt", contains={"a.txt": "required content"}),
                        accept_files("a.txt", sha256={"a.txt": "0" * 64})):
            with self.subTest(checker=checker):
                self.assertEqual(self.evaluate(checker).report.verdict, "failed")

    def test_missing_and_thin_files_fail_with_observed_absence(self) -> None:
        missing = self.evaluate(accept_files("missing.txt"))
        self.assertEqual(missing.report.verdict, "failed")
        self.assertEqual(missing.report.evaluated_resource_versions[0].state, "absent")
        self.assertNotIn("content_hash", missing.receipts[0])
        (self.root / "thin.txt").write_text("x", encoding="utf-8")
        self.assertEqual(self.evaluate(accept_files("thin.txt")).report.verdict, "failed")

    def test_invalid_file_configuration_rejected_before_any_read(self) -> None:
        calls = [lambda: accept_files(), lambda: accept_files("../outside"),
                 lambda: accept_files(str(self.root / "absolute")),
                 lambda: accept_files("."), lambda: accept_files("a", "./a"),
                 lambda: accept_files("a", min_bytes=True),
                 lambda: accept_files("a", min_lines=-1),
                 lambda: accept_files("a", contains={"unlisted": "x"}),
                 lambda: accept_files("a", contains={"a": ""}),
                 lambda: accept_files("a", sha256={"a": "not-a-hash"})]
        for call in calls:
            with self.subTest(call=call), self.assertRaises(ValueError):
                call()

    def test_file_and_parent_symlinks_are_rejected(self) -> None:
        outside = make_temp_dir("acceptance-outside")
        try:
            (outside / "secret.txt").write_text("sensitive data not to be read", encoding="utf-8")
            (self.root / "link.txt").symlink_to(outside / "secret.txt")
            (self.root / "folder").symlink_to(outside, target_is_directory=True)
            for name in ("link.txt", "folder/secret.txt"):
                with self.subTest(name=name):
                    result = self.evaluate(accept_files(name))
                    self.assertEqual(result.report.verdict, "error")
                    self.assertEqual(result.report.evaluated_resource_versions, ())
                    self.assertNotIn("sensitive data", json.dumps(result.trace_payload()))
        finally:
            cleanup(outside)

    def test_modification_during_read_is_error_not_stale_pass(self) -> None:
        path = self.root / "changing.txt"
        path.write_text("original content is long enough\n", encoding="utf-8")
        real_read = os.read
        changed = False

        def concurrent_write(fd, size):
            nonlocal changed
            content = real_read(fd, size)
            if content and not changed:
                changed = True
                path.write_text("replacement content after read\n", encoding="utf-8")
            return content

        with patch("miniagent.acceptance.os.read", side_effect=concurrent_write):
            result = self.evaluate(accept_files("changing.txt"))
        self.assertTrue(changed)
        self.assertEqual(result.report.verdict, "error")
        self.assertIn("changed while reading", result.report.reason)

    def test_checker_configuration_is_copied_and_frozen(self) -> None:
        (self.root / "a.txt").write_text("unexpected content, not the required marker\n", encoding="utf-8")
        expectations = {"a.txt": "ORIGINAL"}
        checker = accept_files("a.txt", contains=expectations)
        expectations["a.txt"] = "unexpected"
        self.assertEqual(self.evaluate(checker).report.verdict, "failed")

    def test_explicit_binding_preserves_exact_spec_and_evaluator_version(self) -> None:
        spec = self.spec()
        result = self.evaluate(AcceptanceBinding(spec, lambda _: (True, "内容已检查")))
        self.assertEqual(result.spec, spec)
        self.assertEqual(result.report.spec_ref.version, 2)
        self.assertEqual(result.report.evaluator_ref.version, 3)
        self.assertEqual(result.report.verdict, "passed")

    def test_wrong_binding_task_environment_or_future_time_does_not_invoke(self) -> None:
        changes = [dict(task_id="other-task"),
                   dict(target={"project_id": "project-a", "environment_id": "a-different-environment", "resource_ids": []}),
                   dict(created_at="2999-01-01T00:00:00Z")]
        calls = []
        for change in changes:
            with self.subTest(change=change):
                original = self.spec(**change)
                result = self.evaluate(AcceptanceBinding(original, lambda _: calls.append(True)))
                self.assertEqual(result.report.verdict, "error")
                self.assertEqual(result.report.task_id, "task-a")
                self.assertNotEqual(result.spec.spec_id, original.spec_id)
                self.assertEqual(result.receipts[0]["rejected_spec"]["spec_id"], original.spec_id)
                self.assertFalse(result.receipts[0]["invoked"])
                validate_report(result.spec, result.report)
        self.assertEqual(calls, [])

    def test_model_copy_cannot_bypass_explicit_spec_validation(self) -> None:
        valid = self.spec()
        invalid_specs = [valid.model_copy(update={"version": True}),
                         valid.model_copy(update={"criteria": ()}),
                         valid.model_copy(update={"params": object()}),
                         valid.model_copy(update={"target": valid.target.model_copy(update={"environment_id": 123})})]
        calls = []
        for invalid in invalid_specs:
            with self.subTest(invalid=invalid):
                result = self.evaluate(AcceptanceBinding(invalid, lambda _: calls.append(True)))
                self.assertEqual(result.report.verdict, "error")
                self.assertIn("invalid binding spec", result.report.reason)
                self.assertEqual(result.report.task_id, "task-a")
                self.assertFalse(result.receipts[0]["invoked"])
                validate_report(result.spec, result.report)
                json.dumps(result.trace_payload())
        self.assertEqual(calls, [])

    def test_no_follow_unavailable_fails_closed_before_read(self) -> None:
        (self.root / "a.txt").write_text("file content long enough\n", encoding="utf-8")
        original_hasattr = hasattr

        def capability(object_, name):
            if object_ is os and name in {"O_NOFOLLOW", "O_DIRECTORY"}:
                return False
            return original_hasattr(object_, name)

        with patch("miniagent.acceptance.hasattr", side_effect=capability, create=True), \
                patch("miniagent.acceptance.os.open") as opened:
            result = self.evaluate(accept_files("a.txt"))
        self.assertEqual(result.report.verdict, "error")
        self.assertIn("unavailable on this platform", result.report.reason)
        opened.assert_not_called()

    def test_multicriterion_binding_covers_optional_failure_without_failing_required(self) -> None:
        spec = self.spec(criteria=[{"id": "required", "description": "必须通过", "required": True},
                                   {"id": "optional", "description": "可选", "required": False}])
        result = self.evaluate(AcceptanceBinding(spec, lambda _: {
            "required": (True, "content verified"), "optional": (False, "no extra notes")}))
        self.assertEqual(result.report.verdict, "passed")
        self.assertEqual([c.verdict for c in result.report.checks], ["passed", "failed"])

    def test_missing_extra_or_collapsed_criteria_are_errors(self) -> None:
        spec = self.spec(criteria=[{"id": "a", "description": "a", "required": True},
                                   {"id": "b", "description": "b", "required": False}])
        for value in ({"a": (True, "yes")}, {"a": (True, "yes"), "b": (True, "yes"), "extra": (True, "yes")},
                      (True, "cannot collapse two checks")):
            with self.subTest(value=value):
                result = self.evaluate(AcceptanceBinding(spec, lambda _, output=value: output))
                self.assertEqual(result.report.verdict, "error")
                self.assertEqual({c.criterion_id for c in result.report.checks}, {"a", "b"})

    def test_required_outputs_or_non_deterministic_evaluator_stay_unverified(self) -> None:
        calls = []
        for change in (dict(required_output_roles=["diagnosis"]),
                       dict(evaluator_ref={"id": "judge", "version": 1, "kind": "llm"})):
            with self.subTest(change=change):
                result = self.evaluate(AcceptanceBinding(self.spec(**change), lambda _: calls.append(True)))
                self.assertEqual(result.report.verdict, "unverified")
                self.assertIsNone(result.report.evaluator_ref)
        self.assertEqual(calls, [])

    def test_explicit_resource_targets_require_observed_versions(self) -> None:
        name = "result.txt"
        (self.root / name).write_text("correct final result content\n", encoding="utf-8")
        spec = self.spec(target={"project_id": "project-a", "environment_id": workspace_environment_id(self.root),
                                 "resource_ids": [workspace_resource_id(self.root, name)]})
        unsupported = self.evaluate(AcceptanceBinding(spec, lambda _: (True, "claimed")))
        self.assertEqual(unsupported.report.verdict, "unverified")
        checked = self.evaluate(AcceptanceBinding(spec, accept_files(name, contains={name: "correct final"})))
        self.assertEqual(checked.report.verdict, "passed")
        self.assertEqual(checked.report.evaluated_resource_versions[0].resource_id, spec.target.resource_ids[0])

    def test_file_binding_cannot_omit_a_target_resource(self) -> None:
        (self.root / "a.txt").write_text("long enough actual file content\n", encoding="utf-8")
        spec = self.spec(target={"project_id": "project-a", "environment_id": workspace_environment_id(self.root),
                                 "resource_ids": [workspace_resource_id(self.root, "b.txt")]})
        result = self.evaluate(AcceptanceBinding(spec, accept_files("a.txt")))
        self.assertEqual(result.report.verdict, "error")
        self.assertIn("target resources", result.report.reason)

    def test_invalid_evaluator_or_workspace_produces_error_report(self) -> None:
        result = self.evaluate(AcceptanceBinding(self.spec(), None))
        self.assertEqual(result.report.verdict, "error")
        result = evaluate(lambda _: (True, "yes"), self.root / "missing", task_id="task-a",
                          attempt_id="attempt-a", objective="check")
        self.assertEqual(result.report.verdict, "error")


if __name__ == "__main__":
    unittest.main()
