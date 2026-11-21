"""Host-owned acceptance adapters for the existing, in-process runtime.

Reports describe checks actually invoked here. Runtime-local callback identities
are not durable evaluator registrations; file observations are not ArtifactStore
registrations or authorization grants. No model output chooses its own checker.
The file adapter requires POSIX no-follow directory-descriptor operations;
Windows currently supports host callbacks, but this built-in file adapter fails
closed there. Portable stat-then-open is not a safe substitute for no-follow.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .contracts.acceptance import (
    AcceptanceReport, AcceptanceSpec, CheckResult, Criterion, EvaluatorRef,
    Target, validate_report,
)
from .contracts.base import (
    AbsentResource, EvidenceRef, PresentResource, VersionRef,
    freeze_json, sha256_bytes, thaw_json, time_value,
)

Acceptance = Callable[[Path], tuple[bool, str]]
Evaluator = Callable[[Path], tuple[bool, str] | dict[str, tuple[bool, str]]]


@dataclass(frozen=True)
class AcceptanceBinding:
    """A trusted caller's spec and checker, fixed for this invocation.

One criterion accepts the legacy tuple result. Multiple criteria require a dict
containing every criterion ID exactly once, with a strict (bool, reason) tuple.
Only deterministic in-process checkers are supported by this adapter.
"""

    spec: AcceptanceSpec
    evaluator: Evaluator


AcceptanceInput = Acceptance | AcceptanceBinding | None


@dataclass(frozen=True)
class AcceptanceEvaluation:
    spec: AcceptanceSpec | None
    report: AcceptanceReport
    receipts: tuple[dict[str, Any], ...]

    def trace_payload(self) -> dict[str, Any]:
        return {
            "spec": self.spec.model_dump(mode="json") if self.spec else None,
            "report": self.report.model_dump(mode="json"),
            "receipts": thaw_json(self.receipts),
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def workspace_environment_id(workspace: Path) -> str:
    """Name this local workspace, never the user's host or a remote environment."""
    digest = hashlib.sha256(str(Path(workspace).resolve()).encode("utf-8")).hexdigest()
    return f"runtime-workspace:{digest}"


def _relative_name(name: str) -> str:
    if type(name) is not str or not name.strip():
        raise ValueError("file names must be nonblank relative strings")
    path = Path(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("file name escapes the workspace")
    return path.as_posix()


def workspace_resource_id(workspace: Path, name: str) -> str:
    name = _relative_name(name)
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()
    return f"{workspace_environment_id(workspace)}:file:{digest}"


def _signature(value: os.stat_result) -> tuple[int, ...]:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


def _parent_fd(root_fd: int, parts: tuple[str, ...]) -> int:
    current = os.dup(root_fd)
    try:
        for part in parts:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def missing_file_probe(binding: AcceptanceBinding | None, workspace: Path,
                       assigned_paths: tuple[str, ...] = ()) -> Callable[[], list[str]] | None:
    """Inspect declared output paths on demand, without invoking an acceptance check."""
    checker = binding.evaluator if binding else None
    declared = checker.names if isinstance(checker, _FilesAcceptance) else getattr(checker, "required_paths", ())
    names = tuple(dict.fromkeys(_relative_name(name) for name in (*assigned_paths, *declared)))
    if not names:
        return None

    def missing() -> list[str]:
        absent = []
        if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
            raise OSError("no-follow file inspection is unavailable")
        root_fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for name in names:
                parts = Path(name).parts
                try:
                    parent = _parent_fd(root_fd, parts[:-1])
                    try:
                        info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                    finally:
                        os.close(parent)
                    if not stat.S_ISREG(info.st_mode):
                        absent.append(name)
                except (FileNotFoundError, NotADirectoryError):
                    absent.append(name)
        finally:
            os.close(root_fd)
        return absent

    return missing


def _read_regular(workspace: Path, name: str) -> bytes | None:
    """Read a stable regular file through no-follow directory descriptors.

This detects observed replacement/modification, not arbitrary future writes or
an OS sandbox. Unsupported platforms fail closed instead of following links.
"""
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise ValueError("stable no-follow file checking is unavailable on this platform")
    root = workspace.resolve(strict=True)
    if workspace.is_symlink():
        raise ValueError("workspace must not be a symbolic link")
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    parts = Path(name).parts
    parent_fd = file_fd = verify_fd = None
    try:
        root_before = os.fstat(root_fd)
        try:
            parent_fd = _parent_fd(root_fd, parts[:-1])
            before = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"not a regular file (symbolic links are forbidden): {name}")
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        if _signature(before) != _signature(os.fstat(file_fd)):
            raise ValueError(f"file changed before reading: {name}")
        chunks: list[bytes] = []
        while chunk := os.read(file_fd, 65536):
            chunks.append(chunk)
        content = b"".join(chunks)
        verify_fd = _parent_fd(root_fd, parts[:-1])
        after = os.stat(parts[-1], dir_fd=verify_fd, follow_symlinks=False)
        root_after = os.stat(root, follow_symlinks=False)
        if (_signature(before) != _signature(os.fstat(file_fd))
                or _signature(before) != _signature(after) or len(content) != before.st_size
                or not stat.S_ISDIR(root_after.st_mode)
                or (root_before.st_dev, root_before.st_ino) != (root_after.st_dev, root_after.st_ino)):
            raise ValueError(f"file changed while reading: {name}")
        return content
    finally:
        for descriptor in (verify_fd, file_fd, parent_fd, root_fd):
            if descriptor is not None:
                os.close(descriptor)


@dataclass(frozen=True)
class _FilesAcceptance:
    names: tuple[str, ...]
    min_bytes: int
    min_lines: int
    contains: dict[str, str]
    hashes: dict[str, str]

    def __call__(self, workspace: Path) -> tuple[bool, str]:
        result, _, _ = self.inspect(Path(workspace))
        return result

    def params(self) -> dict[str, Any]:
        return {"files": list(self.names), "min_bytes": self.min_bytes,
                "min_lines": self.min_lines, "contains": dict(self.contains),
                "sha256": dict(self.hashes)}

    def inspect(self, workspace: Path) -> tuple[tuple[bool, str], list[dict], list[Any]]:
        failures: list[str] = []
        receipts: list[dict] = []
        resources: list[Any] = []
        for name in self.names:
            resource_id = workspace_resource_id(workspace, name)
            content = _read_regular(workspace, name)
            receipt = {"id": _id("file-observation"), "kind": "file_observation",
                       "runtime_local": True, "environment_id": workspace_environment_id(workspace),
                       "resource_id": resource_id, "relative_path": name, "observed_at": _now()}
            if content is None:
                failures.append(f"缺少产物：{name}")
                resources.append(AbsentResource(resource_id=resource_id, state="absent"))
                receipt.update(state="absent")
            else:
                digest = sha256_bytes(content)
                resources.append(PresentResource(resource_id=resource_id, state="present", version=digest))
                receipt.update(state="present", content_hash=digest, size_bytes=len(content))
                text = content.decode("utf-8") if self.min_lines or name in self.contains else ""
                if len(content) < self.min_bytes or len(text.splitlines()) < self.min_lines:
                    failures.append(f"产物内容过少：{name}")
                if name in self.contains and self.contains[name] not in text:
                    failures.append(f"产物内容不符：{name}")
                if name in self.hashes and digest != self.hashes[name]:
                    failures.append(f"SHA-256 不符：{name}")
            receipts.append(receipt)
        return (not failures, "；".join(failures) if failures else "产物齐全"), receipts, resources


def accept_files(
    *names: str, min_bytes: int = 20, min_lines: int = 1,
    contains: dict[str, str] | None = None, sha256: dict[str, str] | None = None,
) -> Acceptance:
    """Check stable file bytes; only configured content/hash checks prove content.

Names must be relative and unique. Hash expectations accept 64 lowercase hex
digits, with or without ``sha256:``. The returned object remains a Path callback.
"""
    normalized = tuple(_relative_name(name) for name in names)
    if not normalized or len(set(normalized)) != len(normalized):
        raise ValueError("at least one unique file name is required")
    if any(type(value) is not int or value < 0 for value in (min_bytes, min_lines)):
        raise ValueError("min_bytes and min_lines must be nonnegative integers")
    checks: list[dict[str, str]] = []
    for supplied in (contains, sha256):
        if supplied is not None and type(supplied) is not dict:
            raise ValueError("content/hash expectations must be dictionaries")
        result: dict[str, str] = {}
        for name, value in (supplied or {}).items():
            key = _relative_name(name)
            if key not in normalized or key in result or type(value) is not str or not value:
                raise ValueError("expectations must name configured files and nonempty strings")
            result[key] = value
        checks.append(result)
    substrings, hashes = checks
    for name, value in hashes.items():
        if not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", value):
            raise ValueError("expected a lowercase SHA-256 digest")
        hashes[name] = value if value.startswith("sha256:") else f"sha256:{value}"
    return _FilesAcceptance(normalized, min_bytes, min_lines, freeze_json(substrings), freeze_json(hashes))


def _runtime_spec(task_id: str, objective: str, workspace: Path, evaluator: Any) -> AcceptanceSpec:
    token = _id("runtime-local")
    files = evaluator if isinstance(evaluator, _FilesAcceptance) else None
    return AcceptanceSpec(
        created_at=_now(), spec_id=f"{token}:spec", version=1, task_id=task_id,
        objective=objective,
        target=Target(project_id=f"runtime-local:{workspace_environment_id(workspace)}",
                      environment_id=workspace_environment_id(workspace),
                      resource_ids=tuple(workspace_resource_id(workspace, name) for name in files.names) if files else ()),
        criteria=(Criterion(id="result", description="执行调用方配置的验收检查", required=True),),
        constraints_ref=VersionRef(id=f"{token}:constraints", version=1),
        evaluator_ref=EvaluatorRef(id="runtime-local:file-checks" if files else f"{token}:callback",
                                   version=1, kind="deterministic"),
        params={"runtime_local": True, "adapter_version": 1,
                "evidence_scope": "observed_file_bytes" if files else "host_callback_return",
                "checks": files.params() if files else {}},
        required_output_roles=(), authority_ref="runtime-local:trusted-caller",
    )


def _strict_result(value: Any) -> tuple[bool, str]:
    if (type(value) is not tuple or len(value) != 2 or type(value[0]) is not bool
            or type(value[1]) is not str or not value[1].strip()):
        raise ValueError("checker must return an exact (bool, nonblank str) tuple")
    return value


def bind(
    acceptance: AcceptanceInput, workspace: Path, *, task_id: str, objective: str,
) -> AcceptanceBinding | None:
    """Freeze a callable's runtime-local spec once before a run or repair loop.

Explicit bindings are returned unchanged. Validation happens in ``evaluate``
so invalid binding configuration becomes an error report, not a setup crash.
"""
    if acceptance is None or isinstance(acceptance, AcceptanceBinding):
        return acceptance
    return AcceptanceBinding(_runtime_spec(task_id, objective, Path(workspace), acceptance), acceptance)


def _finish(
    spec: AcceptanceSpec | None, *, task_id: str, attempt_id: str,
    checks: list[CheckResult], receipts: list[dict], resources: list[Any],
    invoked: bool, reason: str | None = None,
) -> AcceptanceEvaluation:
    verdicts = {check.verdict for check in checks if spec and
                next(c.required for c in spec.criteria if c.id == check.criterion_id)}
    verdict = next((v for v in ("failed", "error", "unverified") if v in verdicts), "passed") if spec else "unverified"
    timestamp = _now()
    report = AcceptanceReport(
        created_at=timestamp, report_id=_id("acceptance-report"), task_id=task_id,
        attempt_id=attempt_id, spec_ref=VersionRef(id=spec.spec_id, version=spec.version) if spec else None,
        evaluator_ref=spec.evaluator_ref if spec and invoked else None,
        verdict=verdict, checks=tuple(checks), evaluated_artifact_refs=(),
        evaluated_resource_versions=tuple(resources),
        evidence_refs=tuple(EvidenceRef(kind="receipt", id=r["id"]) for r in receipts),
        reason=None if verdict == "passed" else (reason or "; ".join(c.reason for c in checks if c.verdict != "passed") or "未配置验收"),
        evaluated_at=timestamp,
    )
    validate_report(spec, report)
    return AcceptanceEvaluation(spec, report, tuple(freeze_json(r) for r in receipts))


def evaluate(
    acceptance: AcceptanceInput, workspace: Path, *, task_id: str,
    attempt_id: str, objective: str,
) -> AcceptanceEvaluation:
    """Invoke trusted checks and return a validated report with actual receipts.

Invalid bindings produce a runtime-local configuration-error spec/report; the
rejected original spec is retained in the receipt, never silently rewritten.
Artifact output roles and human/LLM evaluators remain explicitly unsupported.
"""
    workspace = Path(workspace)
    if acceptance is None:
        return _finish(None, task_id=task_id, attempt_id=attempt_id, checks=[],
                       receipts=[], resources=[], invoked=False, reason="未配置验收 (no-acceptance)")
    binding = bind(acceptance, workspace, task_id=task_id, objective=objective)
    evaluator = binding.evaluator
    spec = binding.spec
    receipts: list[dict] = []
    resources: list[Any] = []
    invocation = {"id": _id("acceptance-invocation"), "kind": "acceptance_invocation",
                  "runtime_local": True, "task_id": task_id, "attempt_id": attempt_id,
                  "environment_id": workspace_environment_id(workspace), "started_at": _now(),
                  "invoked": False}
    invoked = False
    try:
        try:
            if not isinstance(spec, AcceptanceSpec):
                raise ValueError("binding requires an AcceptanceSpec")
            spec = AcceptanceSpec.model_validate(spec)
        except Exception as exc:
            if isinstance(spec, AcceptanceSpec):
                try:
                    invocation["rejected_spec"] = spec.model_dump(mode="json", warnings=False)
                except Exception:
                    invocation["rejected_spec_error"] = "invalid spec is not JSON serializable"
            spec = _runtime_spec(task_id, objective, workspace, None)
            raise ValueError(f"invalid binding spec: {exc}") from exc
        if (spec.task_id != task_id or spec.target.environment_id != workspace_environment_id(workspace)
                or time_value(spec.created_at) > time_value(_now())):
            invocation["rejected_spec"] = spec.model_dump(mode="json")
            spec = _runtime_spec(task_id, objective, workspace, None)
            raise ValueError("binding task/environment/creation time does not match this evaluation")
        if not callable(evaluator):
            raise ValueError("evaluator must be callable")
        if not workspace.is_dir():
            raise ValueError("evaluation workspace must be an existing directory")
        unsupported = None
        if spec.evaluator_ref.kind != "deterministic":
            unsupported = "this adapter cannot execute human/LLM evaluators"
        elif spec.required_output_roles:
            unsupported = "required output roles need registered ArtifactStore evidence"
        elif spec.target.resource_ids and not isinstance(evaluator, _FilesAcceptance):
            unsupported = "callback does not capture the target resource versions"
        if unsupported:
            invocation.update(outcome="unverified", reason=unsupported, ended_at=_now())
            checks = [CheckResult(criterion_id=c.id, verdict="unverified", reason=unsupported, evidence_refs=()) for c in spec.criteria]
            return _finish(spec, task_id=task_id, attempt_id=attempt_id, checks=checks,
                           receipts=[invocation], resources=[], invoked=False, reason=unsupported)
        invoked = invocation["invoked"] = True
        if isinstance(evaluator, _FilesAcceptance):
            raw, observed, resources = evaluator.inspect(workspace)
            receipts.extend(observed)
            if not set(spec.target.resource_ids).issubset({value.resource_id for value in resources}):
                raise ValueError("file checks do not cover the spec target resources")
        else:
            raw = evaluator(workspace)
        if type(raw) is dict:
            if set(raw) != {c.id for c in spec.criteria}:
                raise ValueError("checker results must cover every criterion exactly once")
            results = {key: _strict_result(value) for key, value in raw.items()}
        elif len(spec.criteria) == 1:
            results = {spec.criteria[0].id: _strict_result(raw)}
        else:
            raise ValueError("multiple criteria require a result dictionary")
        invocation.update(outcome="returned", results=results, ended_at=_now())
        receipts.append(invocation)
        refs = tuple(EvidenceRef(kind="receipt", id=r["id"]) for r in receipts)
        checks = [CheckResult(criterion_id=c.id, verdict="passed" if results[c.id][0] else "failed",
                              reason=results[c.id][1], evidence_refs=refs) for c in spec.criteria]
    except Exception as exc:
        if not isinstance(spec, AcceptanceSpec):
            spec = _runtime_spec(task_id, objective, workspace, None)
        reason = f"验收函数异常：{type(exc).__name__}: {exc}"
        invocation.update(outcome="error", reason=reason, ended_at=_now())
        receipts.append(invocation)
        checks = [CheckResult(criterion_id=c.id, verdict="error", reason=reason, evidence_refs=()) for c in spec.criteria]
    return _finish(spec, task_id=task_id, attempt_id=attempt_id, checks=checks,
                   receipts=receipts, resources=resources, invoked=invoked)


__all__ = ["Acceptance", "AcceptanceBinding", "AcceptanceEvaluation", "AcceptanceInput",
           "accept_files", "bind", "evaluate", "workspace_environment_id", "workspace_resource_id"]
