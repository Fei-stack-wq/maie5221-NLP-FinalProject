"""Trusted host lifecycle operations over the M0-04 durable task subset.

Transactions contain record validation and SQLite writes only. No provider,
checker, tool, authorization or external effect runs here. Context/constraint
blobs and reserved budget IDs are not grants or BudgetLedger accounts. Prepared
ActionIdentity blobs identify requests; they never claim execution or approval.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from .acceptance_evidence import validate_evidence
from .contracts.acceptance import AcceptanceReport, AcceptanceSpec, validate_report
from .contracts.base import VersionRef, freeze_json, thaw_json, time_value
from .contracts.tasks import AttemptRecord, RunRecord, TaskRecord
from .contracts.transitions import TransitionError, transition_record
from .task_store import StoreError, StoreTransaction, TaskStore


_CLOSED = {"completed", "unverified", "failed", "cancelled"}


@dataclass(frozen=True)
class ExecutionHandle:
    task: TaskRecord
    attempt: AttemptRecord
    run: RunRecord


def _check(condition: bool, message: str, code: str = "integrity_error") -> None:
    if not condition:
        raise StoreError(message, code=code)


def _text(value: Any, name: str) -> str:
    _check(type(value) is str and bool(value.strip()), f"{name} must be a nonblank string")
    return value


def _json(value: Any) -> Any:
    try:
        return thaw_json(freeze_json(value))
    except (ValueError, TypeError, RecursionError) as exc:
        raise StoreError("request must contain finite JSON values with string keys") from exc


def _digest(value: Any) -> str:
    try:
        data = json.dumps(_json(value), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError) as exc:
        raise StoreError("request cannot be encoded as finite JSON") from exc
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _now(*floors: str) -> str:
    now = datetime.now(timezone.utc)
    if floors:
        now = max(now, *(time_value(value) for value in floors))
    return now.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _revision(task: TaskRecord, expected: int) -> None:
    _check(type(expected) is int and expected == task.revision,
           "stale expected_revision", "revision_conflict")


def _optional_blob(tx: StoreTransaction, identity: str) -> Any:
    try:
        return tx.get_blob(identity)
    except StoreError as exc:
        if exc.code != "not_found":
            raise
        return None


def _update(tx: StoreTransaction, record: Any, state: str, at: str, **changes: Any) -> Any:
    try:
        updated = transition_record(record, state, expected_revision=record.revision, at=at, **changes)
    except (ValueError, TypeError) as exc:
        code = "revision_conflict" if isinstance(exc, TransitionError) and exc.code == "revision_conflict" else "integrity_error"
        raise StoreError("invalid lifecycle transition", code=code) from exc
    return tx.update(updated, expected_revision=record.revision)


def _task_event(tx: StoreTransaction, kind: str, task: TaskRecord, **payload: Any) -> None:
    tx.emit(kind, task.task_id, {"root_id": task.root_id, "parent_id": task.parent_id,
            "revision": task.revision, "status": task.status,
            "attempt_id": task.current_attempt_id, **payload})


class TaskService:
    def __init__(self, store: TaskStore) -> None:
        self.store = store

    def create(
        self, *, objective: str, creation_key: str, owner: str | None,
        assignee: str | None, task_id: str | None = None,
        parent_id: str | None = None, deps: Iterable[str] = (), project_id: str = "local",
    ) -> TaskRecord:
        with self.store.transaction() as tx:
            return self._create(tx, objective=objective, creation_key=creation_key, owner=owner,
                                assignee=assignee, task_id=task_id, parent_id=parent_id,
                                deps=deps, project_id=project_id)

    def _create(
        self, tx: StoreTransaction, *, objective: str, creation_key: str,
        owner: str | None, assignee: str | None, task_id: str | None,
        parent_id: str | None, deps: Iterable[str], project_id: str,
    ) -> TaskRecord:
        for name, value in (("objective", objective), ("creation_key", creation_key), ("project_id", project_id)):
            _text(value, name)
        for name, value in (("owner", owner), ("assignee", assignee), ("task_id", task_id), ("parent_id", parent_id)):
            if value is not None:
                _text(value, name)
        _check(isinstance(deps, (tuple, list)), "deps must be an array")
        dependencies = tuple(_text(value, "dependency") for value in deps)
        _check(len(dependencies) == len(set(dependencies)), "duplicate dependency")
        dependencies = tuple(sorted(dependencies))
        key = "task-create:" + _digest([project_id, creation_key])
        signature = _digest({"objective": objective, "owner": owner, "assignee": assignee,
                             "parent_id": parent_id, "deps": dependencies, "project_id": project_id})
        previous = tx.creation(key)
        if previous is not None:
            _check(previous["signature"] == signature, "creation key conflicts with the semantic request", "conflict")
            _check(task_id is None or task_id == previous["task_id"], "creation key has a different task ID", "conflict")
            return tx.get("TaskRecord", previous["task_id"])
        parent = tx.get("TaskRecord", parent_id) if parent_id is not None else None
        if parent is not None:
            _check(parent.status not in _CLOSED, "cannot create work under a closed parent", "conflict")
            _check(parent.project_id == project_id, "parent belongs to a different project")
        identity = task_id or _id("task")
        _check(identity != parent_id and identity not in dependencies, "task cannot depend on or parent itself")
        at = _now(*([parent.created_at] if parent is not None else []))
        constraint_id = _id("task-constraints")
        task = TaskRecord(
            created_at=at, updated_at=at, revision=1, task_id=identity,
            root_id=parent.root_id if parent is not None else identity, parent_id=parent_id,
            creation_key=key, objective=objective, project_id=project_id,
            owner_profile_id=owner, assignee_profile_id=assignee, deps=dependencies,
            constraints_ref=VersionRef(id=constraint_id, version=1), acceptance_ref=None,
            budget_scope_id=_id("budget-scope"), input_artifact_refs=(), output_artifact_refs=(),
            status="created", reason=None, current_attempt_id=None, latest_report_id=None, wait_ref=None,
        )
        tx.insert(task)
        tx.put_blob(constraint_id, "task_constraints", {
            "id": constraint_id, "version": 1, "task_id": identity, "project_id": project_id,
            "runtime_context_only": True, "constraints": {},
        })
        tx.bind_creation(key, signature, identity)
        _task_event(tx, "task_created", task)
        return task

    def create_plan(self, parent_id: str, definitions: list[dict]) -> list[TaskRecord]:
        _text(parent_id, "parent_id")
        _check(type(definitions) is list and bool(definitions), "plan requires a nonempty array")
        normalized = []
        for definition in definitions:
            _check(type(definition) is dict and set(definition) <= {"id", "objective", "assignee", "deps"},
                   "unsupported plan fields")
            local = _text(definition.get("id"), "plan id")
            objective = _text(definition.get("objective"), "objective")
            assignee = _text(definition.get("assignee"), "assignee")
            deps = definition.get("deps", [])
            _check(isinstance(deps, (list, tuple)), "plan deps must be an array")
            deps = [_text(value, "dependency") for value in deps]
            _check(len(deps) == len(set(deps)), "duplicate plan dependency")
            normalized.append({"id": local, "objective": objective, "assignee": assignee, "deps": sorted(deps)})
        ids = {item["id"] for item in normalized}
        _check(len(ids) == len(normalized), "duplicate plan ID")
        _check(all(set(item["deps"]) <= ids for item in normalized), "plan dependency does not exist")
        with self.store.transaction() as tx:
            parent = tx.get("TaskRecord", parent_id)
            signature = _digest({"parent_id": parent_id, "owner": parent.owner_profile_id,
                                 "project_id": parent.project_id, "definitions": sorted(normalized, key=lambda item: item["id"])})
            plan_id = "task-plan:" + _digest(parent_id)
            existing = _optional_blob(tx, plan_id)
            if existing is not None:
                _check(existing["signature"] == signature, "plan has already been registered with different definitions", "conflict")
                return [tx.get("TaskRecord", f"{parent_id}:{item['id']}") for item in normalized]
            tasks = [self._create(tx, objective=item["objective"],
                                 creation_key="plan:" + _digest([parent_id, item["id"]]),
                                 owner=parent.owner_profile_id, assignee=item["assignee"],
                                 task_id=f"{parent_id}:{item['id']}", parent_id=parent_id,
                                 deps=[f"{parent_id}:{dep}" for dep in item["deps"]], project_id=parent.project_id)
                     for item in normalized]
            tx.put_blob(plan_id, "task_plan", {"parent_id": parent_id, "signature": signature,
                                                "task_ids": [task.task_id for task in tasks]})
            _task_event(tx, "plan_registered", parent, task_ids=[task.task_id for task in tasks])
            return tasks

    @staticmethod
    def _ready(tx: StoreTransaction, task: TaskRecord) -> None:
        _check(task.owner_profile_id is not None and task.assignee_profile_id is not None,
               "ready task requires owner and assignee")
        for dependency in task.deps:
            _check(tx.get("TaskRecord", dependency).status == "completed", "dependency is not completed", "conflict")
        if task.parent_id is not None:
            _check(tx.get("TaskRecord", task.parent_id).status == "running", "parent is not running", "conflict")

    def start(
        self, task_id: str, *, expected_revision: int, context: dict,
        attempt_id: str | None = None, run_id: str | None = None,
    ) -> ExecutionHandle:
        _check(type(context) is dict, "context must be a JSON object")
        context = _json(context)
        for label, value in (("attempt_id", attempt_id), ("run_id", run_id)):
            if value is not None:
                _text(value, label)
        with self.store.transaction() as tx:
            task = tx.get("TaskRecord", task_id)
            _revision(task, expected_revision)
            _check(task.status in {"created", "ready"} and task.current_attempt_id is None,
                   "start only claims unstarted created/ready tasks; retry/resume is not implemented", "conflict")
            _check(not any(record.task_id == task_id for record in tx.list("AttemptRecord")),
                   "task already has execution history", "conflict")
            self._ready(tx, task)
            at = _now(task.updated_at)
            if task.status == "created":
                task = _update(tx, task, "ready", at, reason=None)
                _task_event(tx, "task_ready", task)
            attempt_identity, run_identity, context_identity = attempt_id or _id("attempt"), run_id or _id("run"), _id("task-context")
            tx.put_blob(context_identity, "task_context", {"task_id": task_id, "attempt_id": attempt_identity,
                         "run_id": run_identity, "context": context})
            attempt = AttemptRecord(created_at=at, updated_at=at, revision=1,
                attempt_id=attempt_identity, task_id=task_id, number=1, state="pending",
                context_ref=context_identity, checkpoint_id=None, report_ids=(), started_at=None,
                ended_at=None, end_reason=None)
            run = RunRecord(created_at=at, updated_at=at, revision=1, run_id=run_identity,
                task_id=task_id, attempt_id=attempt_identity, state="prepared", started_at=None,
                ended_at=None, end_reason=None, resumed_from=None)
            tx.insert(attempt)
            tx.insert(run)
            attempt = _update(tx, attempt, "running", at, started_at=at)
            run = _update(tx, run, "running", at, started_at=at)
            task = _update(tx, task, "running", at, current_attempt_id=attempt_identity, reason=None)
            _task_event(tx, "task_started", task, run_id=run_identity, context_ref=context_identity)
            return ExecutionHandle(task, attempt, run)

    def attach_execution(self, handle: ExecutionHandle) -> None:
        """Consume a fresh claim once before an executor's first model call.

        This durable entry marker is neither an execution lease nor a receipt.
        A crash after attachment leaves the run for inspection; it does not
        authorize another executor to replay the same handle automatically.
        """
        try:
            _check(isinstance(handle, ExecutionHandle), "expected an ExecutionHandle")
            expected_task = TaskRecord.model_validate(handle.task)
            expected_attempt = AttemptRecord.model_validate(handle.attempt)
            expected_run = RunRecord.model_validate(handle.run)
        except (ValueError, TypeError) as exc:
            if isinstance(exc, StoreError):
                raise
            raise StoreError("invalid execution handle") from exc
        with self.store.transaction() as tx:
            task = tx.get("TaskRecord", expected_task.task_id)
            attempt, run = self._active(tx, task)
            _check(task.status == "running" and task == expected_task
                   and attempt == expected_attempt and run == expected_run,
                   "execution handle no longer matches the current claim", "conflict")
            identity = "execution-attachment:" + _digest(run.run_id)
            _check(_optional_blob(tx, identity) is None,
                   "execution handle has already been attached; automatic replay is forbidden", "conflict")
            payload = {"task_id": task.task_id, "attempt_id": attempt.attempt_id,
                       "run_id": run.run_id, "attached_at": _now(task.updated_at),
                       "attachment_id": _id("execution-attachment"), "entry_marker_only": True}
            tx.put_blob(identity, "execution_attachment", payload)
            tx.emit("execution_attached", task.task_id, payload)

    @staticmethod
    def _active(tx: StoreTransaction, task: TaskRecord) -> tuple[AttemptRecord, RunRecord]:
        _check(task.status in {"running", "verifying"} and task.current_attempt_id is not None,
               "task does not have an active execution", "conflict")
        attempt = tx.get("AttemptRecord", task.current_attempt_id)
        runs = [run for run in tx.list("RunRecord") if run.attempt_id == attempt.attempt_id and run.state == "running"]
        _check(attempt.task_id == task.task_id and attempt.state == "running" and len(runs) == 1,
               "task/attempt/run claim does not match")
        return attempt, runs[0]

    @staticmethod
    def _descendants_closed(tx: StoreTransaction, task: TaskRecord) -> None:
        family = {task.task_id}
        tasks = tx.list("TaskRecord")
        while True:
            expanded = family | {child.task_id for child in tasks if child.parent_id in family}
            if expanded == family:
                break
            family = expanded
        _check(all(child.status in _CLOSED for child in tasks if child.task_id in family - {task.task_id}),
               "all descendants must be closed before their parent", "conflict")

    def _record_acceptance(
        self, tx: StoreTransaction, task: TaskRecord, spec: AcceptanceSpec | None,
        report: AcceptanceReport, receipts: Iterable[dict], at: str,
    ) -> TaskRecord:
        attempt, _run = self._active(tx, task)
        _check(task.status == "verifying", "acceptance recording requires verifying state", "conflict")
        try:
            report = AcceptanceReport.model_validate(report)
            spec = AcceptanceSpec.model_validate(spec) if spec is not None else None
            validate_report(spec, report)
            receipts = _json(list(receipts))
            _check(report.task_id == task.task_id and report.attempt_id == attempt.attempt_id,
                   "acceptance report belongs to another task/attempt")
            _check(time_value(attempt.started_at) <= time_value(report.evaluated_at)
                   and time_value(report.created_at) <= time_value(at), "acceptance report is outside the active execution time")
            if report.verdict == "passed":
                validate_evidence(spec, report, receipts)
        except (ValueError, TypeError) as exc:
            if isinstance(exc, StoreError):
                raise
            raise StoreError("acceptance specification, report or receipt closure is invalid") from exc
        ref = VersionRef(id=spec.spec_id, version=spec.version) if spec else None
        if task.latest_report_id is not None and task.latest_report_id != report.report_id:
            latest = tx.get("AcceptanceReport", task.latest_report_id)
            _check(report.report_id not in attempt.report_ids
                   and time_value(report.created_at) >= time_value(latest.created_at)
                   and time_value(report.evaluated_at) >= time_value(latest.evaluated_at),
                   "acceptance history cannot move its latest report backwards", "conflict")
        _check(task.acceptance_ref is None or task.acceptance_ref == ref,
               "the active attempt already has a different acceptance specification", "conflict")
        constraints = task.constraints_ref
        if spec is not None:
            _check(spec.task_id == task.task_id, "specification belongs to another task")
            if spec.constraints_ref != constraints:
                _check(task.acceptance_ref is None and spec.params.get("runtime_local") is True
                       and spec.authority_ref == "runtime-local:trusted-caller",
                       "constraints mismatch requires an explicit supported runtime binding", "conflict")
                binding = {"task_id": task.task_id, "attempt_id": attempt.attempt_id,
                           "source_constraints_ref": constraints.model_dump(mode="json"),
                           "bound_constraints_ref": spec.constraints_ref.model_dump(mode="json"),
                           "spec_ref": ref.model_dump(mode="json"), "runtime_compatibility_only": True}
                tx.put_blob("constraints-binding:" + _digest([task.task_id, spec.spec_id, spec.version]),
                            "constraints_binding", binding)
                _task_event(tx, "acceptance_constraints_bound", task, **binding)
                constraints = spec.constraints_ref
            tx.insert(spec)
        tx.insert(report)
        for receipt in receipts:
            _check(type(receipt) is dict, "receipt must be a JSON object")
            receipt_id = _text(receipt.get("id"), "receipt id")
            tx.put_blob(receipt_id, "acceptance_receipt", receipt)
        tx.put_blob("acceptance-evidence:" + report.report_id, "acceptance_evidence", {
            "task_id": task.task_id, "attempt_id": attempt.attempt_id,
            "report_id": report.report_id, "receipts": receipts,
        })
        if report.report_id not in attempt.report_ids:
            _update(tx, attempt, "running", at, report_ids=(*attempt.report_ids, report.report_id))
        if task.latest_report_id == report.report_id and task.acceptance_ref == ref and task.constraints_ref == constraints:
            return task
        task = _update(tx, task, "verifying", at, latest_report_id=report.report_id,
                       acceptance_ref=ref, constraints_ref=constraints)
        _task_event(tx, "acceptance_recorded", task, report_id=report.report_id, verdict=report.verdict,
                    spec_ref=ref.model_dump(mode="json") if ref else None)
        return task

    def record_acceptance(
        self, task_id: str, *, expected_revision: int, spec: AcceptanceSpec | None,
        report: AcceptanceReport, receipts: Iterable[dict] = (),
    ) -> TaskRecord:
        with self.store.transaction() as tx:
            task = tx.get("TaskRecord", task_id)
            _revision(task, expected_revision)
            return self._record_acceptance(tx, task, spec, report, receipts, _now(task.updated_at))

    def finish(
        self, task_id: str, *, expected_revision: int, status: str, error: str = "",
        spec: AcceptanceSpec | None = None, report: AcceptanceReport | None = None,
        receipts: Iterable[dict] = (),
    ) -> TaskRecord:
        _check(type(status) is str and status in _CLOSED, "finish requires a canonical terminal state")
        _check(type(error) is str, "error must be a string")
        _check(status != "completed" or not error, "a task with an execution error cannot complete", "conflict")
        receipts = list(receipts)
        with self.store.transaction() as tx:
            task = tx.get("TaskRecord", task_id)
            _revision(task, expected_revision)
            attempt, run = self._active(tx, task)
            self._descendants_closed(tx, task)
            at = _now(task.updated_at, attempt.updated_at, run.updated_at)
            if report is None and task.latest_report_id is not None:
                report = tx.get("AcceptanceReport", task.latest_report_id)
                if spec is None and report.spec_ref is not None:
                    spec = tx.get("AcceptanceSpec", (report.spec_ref.id, report.spec_ref.version))
            if report is not None and not receipts:
                previous = _optional_blob(tx, "acceptance-evidence:" + report.report_id)
                if previous is not None:
                    receipts = previous["receipts"]
            if report is not None:
                if task.status == "running":
                    task = _update(tx, task, "verifying", at, reason=None)
                    _task_event(tx, "task_verifying", task)
                task = self._record_acceptance(tx, task, spec, report, receipts, at)
                attempt = tx.get("AttemptRecord", attempt.attempt_id)
            else:
                _check(spec is None and not receipts, "spec/receipts require an acceptance report")
            if status in {"completed", "unverified"}:
                expected_verdict = "passed" if status == "completed" else "unverified"
                _check(report is not None and report.verdict == expected_verdict,
                       "terminal state requires its matching acceptance report")
            reason = error.strip() or (report.reason if report is not None else None) or status
            run = _update(tx, run, "ended", at, ended_at=at, end_reason=reason)
            attempt = _update(tx, attempt, "succeeded" if status == "completed" else status,
                              at, ended_at=at, end_reason=reason)
            task = _update(tx, task, status, at, reason=None if status == "completed" else reason, wait_ref=None)
            _task_event(tx, "task_finished", task, run_id=run.run_id,
                        report_id=task.latest_report_id, reason=task.reason)
            return task

    def cancel(self, task_id: str, *, expected_revision: int, reason: str) -> TaskRecord:
        _text(reason, "cancellation reason")
        with self.store.transaction() as tx:
            task = tx.get("TaskRecord", task_id)
            _revision(task, expected_revision)
            _check(task.status in {"created", "ready", "blocked"} and task.current_attempt_id is None,
                   "cancel only handles unstarted created/ready/blocked tasks", "conflict")
            self._descendants_closed(tx, task)
            task = _update(tx, task, "cancelled", _now(task.updated_at), reason=reason)
            _task_event(tx, "task_cancelled", task, reason=reason)
            return task

    def transition(self, task_id: str, *, expected_revision: int, status: str, reason: str | None = None) -> TaskRecord:
        _check(type(status) is str, "status must be a string")
        with self.store.transaction() as tx:
            task = tx.get("TaskRecord", task_id)
            _revision(task, expected_revision)
            if (task.status, status) in {("running", "verifying"), ("verifying", "running")}:
                self._active(tx, task)
                _check(reason is None, "running/verifying transitions do not carry a failure reason")
            else:
                _check(task.status in {"created", "ready", "blocked"} and task.current_attempt_id is None
                       and status in {"ready", "blocked", "cancelled"},
                       "execution/terminal updates require coupled service operations", "conflict")
                if status == "ready":
                    self._ready(tx, task)
                    _check(reason is None, "ready task must clear its reason")
                else:
                    _text(reason, "transition reason")
                if status == "cancelled":
                    self._descendants_closed(tx, task)
            task = _update(tx, task, status, _now(task.updated_at), reason=reason, wait_ref=None)
            _task_event(tx, "task_transitioned", task, reason=reason)
            return task

    def action_identities(
        self, task_id: str, *, attempt_id: str, run_id: str, step: int, calls: list[dict],
    ) -> list[dict]:
        _check(type(step) is int and step >= 0, "step must be a nonnegative integer")
        _check(type(calls) is list, "calls must be an array")
        normalized = []
        for call in calls:
            _check(type(call) is dict and {"tool_call_id", "operation", "arguments"} <= set(call)
                   and set(call) <= {"tool_call_id", "operation", "arguments", "raw_arguments"}, "invalid action identity fields")
            _check(type(call["arguments"]) is dict, "action arguments must be a JSON object")
            raw = call.get("raw_arguments")
            _check(raw is None or type(raw) is str, "raw_arguments must be a string or null")
            normalized.append({"tool_call_id": _text(call["tool_call_id"], "tool_call_id"),
                "operation": _text(call["operation"], "operation"), "arguments": _json(call["arguments"]),
                "raw_arguments": raw})
        _check(len({call["tool_call_id"] for call in normalized}) == len(normalized), "duplicate tool_call_id in batch")
        with self.store.transaction() as tx:
            task = tx.get("TaskRecord", task_id)
            attempt, run = self._active(tx, task)
            _check(task.status == "running" and (attempt.attempt_id, run.run_id) == (attempt_id, run_id),
                   "action identity does not match the active execution", "conflict")
            batch_id = "action-batch:" + _digest([task_id, attempt_id, run_id, step])
            signature = _digest(normalized)
            existing = _optional_blob(tx, batch_id)
            if existing is not None:
                _check(existing["signature"] == signature, "run/step already has a different action batch", "conflict")
                return existing["identities"]
            identities = []
            for call in normalized:
                identity = {"action_id": _id("action"), "task_id": task_id, "attempt_id": attempt_id,
                    "run_id": run_id, "step": step, **call, "signature": _digest(call),
                    "status": "prepared", "identity_only": True}
                tx.put_blob("action-identity:" + _digest([run_id, step, call["tool_call_id"]]), "action_identity", identity)
                tx.emit("action_identity_prepared", task_id, {key: identity[key] for key in (
                    "action_id", "attempt_id", "run_id", "step", "tool_call_id", "operation", "signature", "identity_only")})
                identities.append(identity)
            tx.put_blob(batch_id, "action_batch", {"signature": signature, "identities": identities})
            return identities

    def resume_candidates(self) -> list[TaskRecord]:
        """List open records for host inspection; none are approved for replay."""
        return [task for task in self.store.tasks() if task.status not in _CLOSED]


__all__ = ["ExecutionHandle", "TaskService"]
