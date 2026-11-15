"""Immutable acceptance requirements and evidence snapshots.

These contracts validate recorded assertions; they do not run evaluators or
decide whether a task may transition to completed.
"""
from __future__ import annotations

from typing import Literal

from pydantic import model_validator

from .base import (
    Array, ArtifactRef, ContractModel, EvidenceRef, Id, JsonObject,
    PositiveInt, Record, ResourceVersion, Text, Timestamp, VersionRef,
    require, time_value, unique,
)


Verdict = Literal["passed", "failed", "unverified", "error"]


class Target(ContractModel):
    project_id: Id
    environment_id: Id
    resource_ids: Array[Id]

    @model_validator(mode="after")
    def unique_resources(self) -> Target:
        unique(self.resource_ids, "target.resource_ids")
        return self


class Criterion(ContractModel):
    id: Id
    description: Text
    required: bool


class EvaluatorRef(ContractModel):
    id: Id
    version: PositiveInt
    kind: Literal["deterministic", "human", "llm"]


class AcceptanceSpec(Record):
    record_type: Literal["AcceptanceSpec"] = "AcceptanceSpec"
    spec_id: Id
    version: PositiveInt
    task_id: Id
    objective: Text
    target: Target
    criteria: Array[Criterion]
    constraints_ref: VersionRef
    evaluator_ref: EvaluatorRef
    params: JsonObject
    required_output_roles: Array[Text]
    authority_ref: Id

    @model_validator(mode="after")
    def valid_criteria(self) -> AcceptanceSpec:
        require(bool(self.criteria), "criteria must not be empty")
        unique(self.criteria, "criteria", key=lambda value: value.id)
        require(any(value.required for value in self.criteria), "at least one criterion must be required")
        unique(self.required_output_roles, "required_output_roles")
        return self


class CheckResult(ContractModel):
    criterion_id: Id
    verdict: Verdict
    reason: Text
    evidence_refs: Array[EvidenceRef]

    @model_validator(mode="after")
    def valid_evidence(self) -> CheckResult:
        unique(self.evidence_refs, "check.evidence_refs", key=lambda value: (value.kind, value.id))
        if self.verdict in {"passed", "failed"}:
            require(bool(self.evidence_refs), "a definite check verdict requires evidence")
        return self


class AcceptanceReport(Record):
    record_type: Literal["AcceptanceReport"] = "AcceptanceReport"
    report_id: Id
    task_id: Id
    attempt_id: Id
    spec_ref: VersionRef | None
    evaluator_ref: EvaluatorRef | None
    verdict: Verdict
    checks: Array[CheckResult]
    evaluated_artifact_refs: Array[ArtifactRef]
    evaluated_resource_versions: Array[ResourceVersion]
    evidence_refs: Array[EvidenceRef]
    reason: Text | None
    evaluated_at: Timestamp

    @model_validator(mode="after")
    def valid_report(self) -> AcceptanceReport:
        unique(self.checks, "checks", key=lambda value: value.criterion_id)
        unique(self.evaluated_artifact_refs, "evaluated_artifact_refs", key=lambda value: value.artifact_id)
        unique(self.evaluated_resource_versions, "evaluated_resource_versions", key=lambda value: value.resource_id)
        unique(self.evidence_refs, "report.evidence_refs", key=lambda value: (value.kind, value.id))
        require(time_value(self.evaluated_at) <= time_value(self.created_at), "evaluated_at follows report creation")
        if self.verdict != "passed":
            require(self.reason is not None, "a non-passed report requires a reason")
        if self.spec_ref is None:
            require(self.verdict == "unverified", "a report without a spec must be unverified")
            require(not self.checks, "a report without a spec cannot contain checks")
            require(self.evaluator_ref is None, "a report without a spec cannot name an evaluator")
        else:
            require(bool(self.checks), "a report with a spec must contain criterion checks")
        if self.verdict in {"passed", "failed"} or any(
            check.verdict in {"passed", "failed"} for check in self.checks
        ):
            require(self.spec_ref is not None, "a definite verdict requires a spec")
            require(self.evaluator_ref is not None, "a definite verdict requires an evaluator")
        return self


def validate_report(spec: AcceptanceSpec | None, report: AcceptanceReport) -> None:
    """Check the exact spec/evaluator binding and required-criterion aggregate.

    Reference existence, task/attempt ownership, current constraints and target
    coverage require the wider contract snapshot and are deliberately separate.
    """
    if spec is None:
        require(report.spec_ref is None, "report references a spec that was not supplied")
        require(report.verdict == "unverified", "a report without a spec must be unverified")
        require(not report.checks, "a report without a spec cannot contain checks")
        require(report.evaluator_ref is None, "a report without a spec cannot name an evaluator")
        return

    require(report.task_id == spec.task_id, "report task_id does not match the spec")
    require(
        report.spec_ref == VersionRef(id=spec.spec_id, version=spec.version),
        "report spec_ref does not match the exact spec version",
    )
    if report.evaluator_ref is not None:
        require(report.evaluator_ref == spec.evaluator_ref, "report evaluator does not match the spec")
    require(time_value(report.evaluated_at) >= time_value(spec.created_at), "evaluation precedes spec creation")
    expected = {criterion.id for criterion in spec.criteria}
    checks = {check.criterion_id: check for check in report.checks}
    require(set(checks) == expected, "report must contain exactly one check for every spec criterion")
    verdicts = {checks[criterion.id].verdict for criterion in spec.criteria if criterion.required}
    aggregate: Verdict = "passed"
    for candidate in ("failed", "error", "unverified"):
        if candidate in verdicts:
            aggregate = candidate
            break
    require(report.verdict == aggregate, "report verdict does not match required-criterion aggregation")


__all__ = [
    "AcceptanceReport", "AcceptanceSpec", "CheckResult", "Criterion",
    "EvaluatorRef", "Target", "Verdict", "validate_report",
]
