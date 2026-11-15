"""Acceptance evidence and artifact identity tests for opt-in contracts."""
from __future__ import annotations

import copy
import unittest

from pydantic import ValidationError

from miniagent.contracts.acceptance import AcceptanceReport, AcceptanceSpec, validate_report
from miniagent.contracts.artifacts import ArtifactGrant, ArtifactRecord, validate_artifact_content
from miniagent.contracts.base import sha256_bytes


CREATED = "2026-11-15T10:00:00Z"
EVALUATED = "2026-11-15T10:01:00Z"
LATER = "2026-11-15T10:02:00Z"
CONTENT = "# 诊断\n解释器选择正确。\n".encode("utf-8")
CONTENT_HASH = sha256_bytes(CONTENT)


def spec_data() -> dict:
    return {
        "created_at": CREATED,
        "spec_id": "S1", "version": 1, "task_id": "T1",
        "objective": "恢复项目启动",
        "target": {"project_id": "P1", "environment_id": "E1", "resource_ids": ["app"]},
        "criteria": [
            {"id": "works", "description": "原始入口可启动", "required": True},
            {"id": "notes", "description": "附加说明完整", "required": False},
        ],
        "constraints_ref": {"id": "C1", "version": 1},
        "evaluator_ref": {"id": "checker", "version": 1, "kind": "deterministic"},
        "params": {"entry": "app", "options": {"smoke": True}},
        "required_output_roles": ["diagnosis"], "authority_ref": "U1",
    }


def check(criterion_id: str, verdict: str) -> dict:
    return {
        "criterion_id": criterion_id, "verdict": verdict, "reason": "检查结果",
        "evidence_refs": [{"kind": "receipt", "id": "RC1"}] if verdict in {"passed", "failed"} else [],
    }


def report_data() -> dict:
    return {
        "created_at": LATER,
        "report_id": "R1", "task_id": "T1", "attempt_id": "AT1",
        "spec_ref": {"id": "S1", "version": 1},
        "evaluator_ref": {"id": "checker", "version": 1, "kind": "deterministic"},
        "verdict": "passed", "checks": [check("works", "passed"), check("notes", "passed")],
        "evaluated_artifact_refs": [{"artifact_id": "F1", "content_hash": CONTENT_HASH}],
        "evaluated_resource_versions": [{"resource_id": "app", "state": "present", "version": CONTENT_HASH}],
        "evidence_refs": [{"kind": "receipt", "id": "RC1"}],
        "reason": None, "evaluated_at": EVALUATED,
    }


def artifact_data() -> dict:
    return {
        "created_at": CREATED, "artifact_id": "F1", "logical_name": "diagnosis.md",
        "role": "diagnosis", "media_type": "text/markdown", "size_bytes": len(CONTENT),
        "content_hash": CONTENT_HASH, "storage_key": "objects/f1",
        "creator_ref": {"kind": "task", "task_id": "T1", "attempt_id": "AT1", "action_id": None},
        "input_refs": [], "source_refs": [{"kind": "receipt", "id": "RC1"}],
        "scope": {"project_id": "P1", "root_task_id": "T1"},
    }


def grant_data() -> dict:
    return {
        "created_at": CREATED, "updated_at": CREATED, "revision": 1,
        "grant_id": "G1", "subject": {"kind": "profile", "id": "maintainer"},
        "artifact_ids": ["F1"], "task_ids": ["T1"], "project_id": "P1",
        "operations": ["read", "materialize"], "authority_ref": "U1",
        "expires_at": None, "status": "active",
    }


class AcceptanceContractTest(unittest.TestCase):
    def test_valid_roundtrip_and_optional_failure_does_not_fail_report(self) -> None:
        spec = AcceptanceSpec.model_validate(spec_data())
        data = report_data()
        data["checks"][1] = check("notes", "failed")
        report = AcceptanceReport.model_validate(data)
        self.assertIsNone(validate_report(spec, report))
        self.assertEqual(AcceptanceReport.model_validate_json(report.model_dump_json()), report)
        self.assertEqual(AcceptanceSpec.model_validate_json(spec.model_dump_json()), spec)

    def test_required_verdict_precedence(self) -> None:
        cases = [
            (["passed", "passed", "passed"], "passed"),
            (["passed", "unverified", "passed"], "unverified"),
            (["unverified", "error", "passed"], "error"),
            (["error", "unverified", "failed"], "failed"),
        ]
        for verdicts, aggregate in cases:
            with self.subTest(verdicts=verdicts):
                spec_raw = spec_data()
                spec_raw["criteria"] = [
                    {"id": str(index), "description": "目标条件", "required": True}
                    for index in range(3)
                ]
                raw = report_data()
                raw["checks"] = [check(str(index), verdict) for index, verdict in enumerate(verdicts)]
                raw["verdict"] = aggregate
                raw["reason"] = None if aggregate == "passed" else "尚未满足"
                self.assertIsNone(validate_report(AcceptanceSpec.model_validate(spec_raw), AcceptanceReport.model_validate(raw)))
                raw["verdict"] = "failed" if aggregate != "failed" else "error"
                raw["reason"] = "错误汇总"
                with self.assertRaises(ValueError):
                    validate_report(AcceptanceSpec.model_validate(spec_raw), AcceptanceReport.model_validate(raw))

    def test_every_criterion_required_in_report_including_optional(self) -> None:
        for checks in ([check("works", "passed")], [check("works", "passed"), check("unknown", "passed")]):
            with self.subTest(checks=checks):
                raw = report_data()
                raw["checks"] = checks
                with self.assertRaises(ValueError):
                    validate_report(AcceptanceSpec.model_validate(spec_data()), AcceptanceReport.model_validate(raw))

    def test_duplicate_criterion_cannot_hide_omission(self) -> None:
        raw = report_data()
        raw["checks"] = [check("works", "passed"), check("works", "passed")]
        with self.assertRaises(ValidationError):
            AcceptanceReport.model_validate(raw)

    def test_exact_task_spec_and_evaluator_binding(self) -> None:
        changes = [
            {"task_id": "T2"}, {"spec_ref": {"id": "S2", "version": 1}},
            {"spec_ref": {"id": "S1", "version": 2}},
            {"evaluator_ref": {"id": "other", "version": 1, "kind": "deterministic"}},
            {"evaluator_ref": {"id": "checker", "version": 2, "kind": "deterministic"}},
            {"evaluator_ref": {"id": "checker", "version": 1, "kind": "llm"}},
        ]
        for change in changes:
            with self.subTest(change=change):
                raw = report_data() | change
                with self.assertRaises(ValueError):
                    validate_report(AcceptanceSpec.model_validate(spec_data()), AcceptanceReport.model_validate(raw))

    def test_no_spec_only_allows_explicit_unverified_report(self) -> None:
        raw = report_data() | {
            "spec_ref": None, "evaluator_ref": None, "checks": [],
            "verdict": "unverified", "reason": "无验收要求",
        }
        self.assertIsNone(validate_report(None, AcceptanceReport.model_validate(raw)))
        for change in ({"verdict": "passed"}, {"checks": [check("works", "unverified")]},
                       {"evaluator_ref": spec_data()["evaluator_ref"]}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                AcceptanceReport.model_validate(raw | change)
        with self.assertRaises(ValueError):
            validate_report(None, AcceptanceReport.model_validate(report_data()))

    def test_unexecuted_checks_may_have_no_evaluator(self) -> None:
        for verdict in ("unverified", "error"):
            raw = report_data() | {
                "evaluator_ref": None, "verdict": verdict, "reason": "检查尚未完成",
                "checks": [check("works", verdict), check("notes", "unverified")],
            }
            self.assertIsNone(validate_report(AcceptanceSpec.model_validate(spec_data()), AcceptanceReport.model_validate(raw)))
            raw["checks"][1] = check("notes", "passed")
            with self.assertRaises(ValidationError):
                AcceptanceReport.model_validate(raw)

    def test_definite_checks_require_evidence(self) -> None:
        for verdict in ("passed", "failed"):
            raw = report_data()
            raw["checks"][0] = check("works", verdict) | {"evidence_refs": []}
            with self.subTest(verdict=verdict), self.assertRaises(ValidationError):
                AcceptanceReport.model_validate(raw)

    def test_nonpassed_report_requires_nonblank_reason(self) -> None:
        for reason in (None, "", "  "):
            raw = report_data() | {"verdict": "failed", "reason": reason}
            with self.subTest(reason=reason), self.assertRaises(ValidationError):
                AcceptanceReport.model_validate(raw)

    def test_spec_requires_unique_and_required_criteria(self) -> None:
        for criteria in ([], [spec_data()["criteria"][1]], [spec_data()["criteria"][0]] * 2):
            with self.subTest(criteria=criteria), self.assertRaises(ValidationError):
                AcceptanceSpec.model_validate(spec_data() | {"criteria": criteria})

    def test_duplicate_reference_ids_are_rejected(self) -> None:
        for field in ("evaluated_artifact_refs", "evaluated_resource_versions", "evidence_refs"):
            raw = report_data()
            raw[field] *= 2
            with self.subTest(field=field), self.assertRaises(ValidationError):
                AcceptanceReport.model_validate(raw)
        for field, value in (("required_output_roles", ["diagnosis", "diagnosis"]),
                             ("target", spec_data()["target"] | {"resource_ids": ["app", "app"]})):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                AcceptanceSpec.model_validate(spec_data() | {field: value})

    def test_explicit_nullable_fields_strict_types_and_frozen_params(self) -> None:
        raw = report_data()
        del raw["reason"]
        with self.assertRaises(ValidationError):
            AcceptanceReport.model_validate(raw)
        raw = spec_data()
        raw["criteria"][0]["required"] = "true"
        with self.assertRaises(ValidationError):
            AcceptanceSpec.model_validate(raw)
        spec = AcceptanceSpec.model_validate(spec_data())
        with self.assertRaises(TypeError):
            spec.params["options"]["smoke"] = False
        with self.assertRaises(ValidationError):
            spec.objective = "改写目标"

    def test_evaluation_time_cannot_precede_spec_or_follow_report(self) -> None:
        raw = report_data() | {"evaluated_at": "2026-11-15T09:59:00Z"}
        with self.assertRaises(ValueError):
            validate_report(AcceptanceSpec.model_validate(spec_data()), AcceptanceReport.model_validate(raw))
        with self.assertRaises(ValidationError):
            AcceptanceReport.model_validate(report_data() | {"evaluated_at": "2026-11-15T10:03:00Z"})


class ArtifactContractTest(unittest.TestCase):
    def test_content_bytes_and_metadata_roundtrip(self) -> None:
        record = ArtifactRecord.model_validate(artifact_data())
        self.assertIsNone(validate_artifact_content(record, CONTENT))
        self.assertEqual(ArtifactRecord.model_validate_json(record.model_dump_json()), record)
        self.assertEqual(record.size_bytes, len(CONTENT))

    def test_size_and_hash_must_both_match_actual_bytes(self) -> None:
        for raw in (artifact_data() | {"size_bytes": len(CONTENT) + 1},
                    artifact_data() | {"content_hash": sha256_bytes(b"different") } ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                validate_artifact_content(ArtifactRecord.model_validate(raw), CONTENT)
        with self.assertRaises(ValueError):
            validate_artifact_content(ArtifactRecord.model_validate(artifact_data()), bytearray(CONTENT))

    def test_empty_content_is_a_real_version(self) -> None:
        raw = artifact_data() | {"size_bytes": 0, "content_hash": sha256_bytes(b"")}
        self.assertIsNone(validate_artifact_content(ArtifactRecord.model_validate(raw), b""))

    def test_creator_union_distinguishes_import_from_task(self) -> None:
        raw = artifact_data() | {"creator_ref": {"kind": "import", "actor": {"kind": "user", "id": "U1"}, "source_id": "MSG1"}}
        self.assertEqual(ArtifactRecord.model_validate(raw).creator_ref.kind, "import")
        for creator in (
            {"kind": "task", "task_id": "T1", "attempt_id": "AT1"},
            {"kind": "import", "actor": {"kind": "user", "id": "U1"}, "source_id": "MSG1", "task_id": "T1"},
            {"kind": "unknown", "task_id": "T1"},
        ):
            with self.subTest(creator=creator), self.assertRaises(ValidationError):
                ArtifactRecord.model_validate(artifact_data() | {"creator_ref": creator})

    def test_artifact_input_provenance_is_unique_and_not_self_referential(self) -> None:
        for refs in (
            [{"artifact_id": "F1", "content_hash": CONTENT_HASH}],
            [{"artifact_id": "F0", "content_hash": CONTENT_HASH}] * 2,
            [{"artifact_id": "F0", "content_hash": CONTENT_HASH}, {"artifact_id": "F0", "content_hash": sha256_bytes(b"other")}],
        ):
            with self.subTest(refs=refs), self.assertRaises(ValidationError):
                ArtifactRecord.model_validate(artifact_data() | {"input_refs": refs})

    def test_evidence_uniqueness_uses_kind_and_id(self) -> None:
        raw = artifact_data()
        raw["source_refs"] = [{"kind": "receipt", "id": "same"}, {"kind": "catalog", "id": "same"}]
        ArtifactRecord.model_validate(raw)
        raw["source_refs"].append(copy.deepcopy(raw["source_refs"][0]))
        with self.assertRaises(ValidationError):
            ArtifactRecord.model_validate(raw)

    def test_size_rejects_bool_and_hash_has_strict_format(self) -> None:
        for change in ({"size_bytes": True}, {"size_bytes": -1}, {"content_hash": "sha256:ABC"}, {"unexpected": "ignored?"}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                ArtifactRecord.model_validate(artifact_data() | change)

    def test_grant_exact_sets_are_nonempty_and_unique(self) -> None:
        for field in ("artifact_ids", "task_ids", "operations"):
            for value in ([], [grant_data()[field][0]] * 2):
                with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                    ArtifactGrant.model_validate(grant_data() | {field: value})
        with self.assertRaises(ValidationError):
            ArtifactGrant.model_validate(grant_data() | {"operations": ["publish"]})

    def test_grant_nullable_expiry_is_required_and_expired_state_has_a_boundary(self) -> None:
        raw = grant_data()
        del raw["expires_at"]
        with self.assertRaises(ValidationError):
            ArtifactGrant.model_validate(raw)
        with self.assertRaises(ValidationError):
            ArtifactGrant.model_validate(grant_data() | {"status": "expired"})
        with self.assertRaises(ValidationError):
            ArtifactGrant.model_validate(grant_data() | {"status": "expired", "expires_at": LATER})
        expired = ArtifactGrant.model_validate(grant_data() | {
            "status": "expired", "expires_at": EVALUATED, "updated_at": LATER, "revision": 2,
        })
        self.assertEqual(ArtifactGrant.model_validate_json(expired.model_dump_json()), expired)
        # Snapshot construction does not consult the wall clock or mutate status.
        active = ArtifactGrant.model_validate(grant_data() | {"expires_at": EVALUATED})
        self.assertEqual(active.status, "active")


if __name__ == "__main__":
    unittest.main()
