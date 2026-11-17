"""Validate the evidence closure recorded by the in-process acceptance adapter.

This checks data consistency, not external truth, authentication, live files or
ArtifactStore access. Only invocation and file-observation receipts are resolved.
Unsupported evidence cannot establish a verified runtime success.
"""
from __future__ import annotations

from typing import Any, Iterable

from pydantic import TypeAdapter

from .contracts.acceptance import AcceptanceReport, AcceptanceSpec, validate_report
from .contracts.base import Timestamp, freeze_json, require, thaw_json, time_value


_TIME = TypeAdapter(Timestamp)


def _time(value: Any):
    return time_value(_TIME.validate_python(value))


def validate_evidence(
    spec: AcceptanceSpec,
    report: AcceptanceReport,
    receipts: Iterable[dict[str, Any]],
) -> None:
    """Require a consistent, fully resolved passed report; otherwise ValueError.

    Task/attempt binding to the caller's current result remains the caller's
    responsibility. Immutable report/spec contracts and receipt identities are
    checked again here so callers cannot bypass validation with model_copy.
    """
    spec = AcceptanceSpec.model_validate(spec)
    report = AcceptanceReport.model_validate(report)
    validate_report(spec, report)
    require(report.verdict == "passed", "evidence verification requires a passed report")
    require(spec.evaluator_ref.kind == "deterministic", "runtime evidence resolver only supports deterministic evaluators")
    require(not spec.required_output_roles and not report.evaluated_artifact_refs,
            "artifact output verification requires an ArtifactStore resolver")
    index: dict[str, dict[str, Any]] = {}
    try:
        supplied = tuple(receipts)
    except TypeError as exc:
        raise ValueError("receipts must be an iterable of receipt objects") from exc
    for value in supplied:
        require(isinstance(value, dict), "receipt must be an object")
        identity = value.get("id")
        require(type(identity) is str and bool(identity.strip()), "receipt requires a nonblank id")
        value = thaw_json(freeze_json(value))
        require(identity not in index or index[identity] == value, "conflicting receipt contents for one id")
        index[identity] = value

    references = list(report.evidence_refs) + [ref for check in report.checks for ref in check.evidence_refs]
    require(bool(references), "passed report has no resolvable evidence")
    used: dict[str, dict[str, Any]] = {}
    for ref in references:
        require(ref.kind == "receipt", f"no resolver for {ref.kind} evidence")
        require(ref.id in index, f"missing evidence receipt: {ref.id}")
        used[ref.id] = index[ref.id]

    checks = {check.criterion_id: check for check in report.checks}
    invocations: dict[str, tuple[Any, Any]] = {}
    observations: list[dict[str, Any]] = []
    for identity, receipt in used.items():
        kind = receipt.get("kind")
        require(receipt.get("environment_id") == spec.target.environment_id, "receipt environment mismatch")
        if kind == "acceptance_invocation":
            require(receipt.get("task_id") == report.task_id and receipt.get("attempt_id") == report.attempt_id,
                    "invocation task/attempt mismatch")
            require(receipt.get("invoked") is True and receipt.get("outcome") == "returned",
                    "checker was not invoked and returned successfully")
            started, ended = _time(receipt.get("started_at")), _time(receipt.get("ended_at"))
            require(_time(spec.created_at) <= started <= ended <= _time(report.evaluated_at),
                    "invocation time falls outside spec/report boundaries")
            results = receipt.get("results")
            require(isinstance(results, dict) and set(results) == set(checks), "invocation results do not cover report checks")
            for criterion_id, result in results.items():
                require(isinstance(result, (list, tuple)) and len(result) == 2
                        and type(result[0]) is bool and type(result[1]) is str and bool(result[1].strip()),
                        "invocation result must be a strict boolean/reason pair")
                check = checks[criterion_id]
                require(check.verdict == ("passed" if result[0] else "failed") and check.reason == result[1],
                        "reported check does not match the invocation result")
            invocations[identity] = (started, ended)
        elif kind == "file_observation":
            observations.append(receipt)
        else:
            raise ValueError(f"unsupported runtime receipt kind: {kind}")
    require(bool(invocations), "passed report requires an invocation receipt")
    for check in report.checks:
        require(any(ref.kind == "receipt" and ref.id in invocations for ref in check.evidence_refs),
                "each check must reference its invocation receipt")

    resources = {value.resource_id: value for value in report.evaluated_resource_versions}
    require(set(spec.target.resource_ids).issubset(resources), "report does not cover target resources")
    observed_ids: set[str] = set()
    for receipt in observations:
        resource_id = receipt.get("resource_id")
        require(type(resource_id) is str and resource_id in resources, "file observation has no matching report resource")
        resource = resources[resource_id]
        require(receipt.get("state") == resource.state, "file observation state differs from report")
        if resource.state == "present":
            require(receipt.get("content_hash") == resource.version, "file observation hash differs from report")
            require(type(receipt.get("size_bytes")) is int and receipt["size_bytes"] >= 0,
                    "present file observation requires a nonnegative byte size")
        else:
            require(receipt.get("content_hash") is None, "absent file cannot have a content hash")
        observed = _time(receipt.get("observed_at"))
        require(any(started <= observed <= ended for started, ended in invocations.values()),
                "file observation was not captured during the invocation")
        observed_ids.add(resource_id)
    require(set(resources).issubset(observed_ids), "report resource versions lack file observations")


__all__ = ["validate_evidence"]
