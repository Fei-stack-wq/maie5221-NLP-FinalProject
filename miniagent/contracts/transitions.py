"""Pure transition checks; no persistence, locks, authorization or execution.

An expected revision check here is not database compare-and-swap. A future store
must perform the same check atomically with the actual write and its outbox.
"""
from __future__ import annotations

from typing import Any, TypeVar

from .base import MutableRecord, time_value


class TransitionError(ValueError):
    def __init__(self, message: str, code: str = "invalid_transition") -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


TASK_TRANSITIONS = {
    "created": {"ready", "blocked", "cancelled"},
    "ready": {"running", "blocked", "failed", "cancelled"},
    "running": {"verifying", "waiting_user", "waiting_agent", "blocked", "failed", "cancelled"},
    "verifying": {"completed", "running", "waiting_user", "unverified", "failed", "cancelled"},
    "waiting_user": {"ready", "failed", "cancelled"},
    "waiting_agent": {"ready", "failed", "cancelled"},
    "blocked": {"ready", "failed", "cancelled"},
    "completed": set(), "unverified": set(), "failed": set(), "cancelled": set(),
}
ATTEMPT_TRANSITIONS = {
    "pending": {"running", "failed", "cancelled"},
    "running": {"suspended", "succeeded", "unverified", "failed", "cancelled"},
    "suspended": {"running", "failed", "cancelled"},
    "succeeded": set(), "unverified": set(), "failed": set(), "cancelled": set(),
}
RUN_TRANSITIONS = {
    "prepared": {"running", "ended"}, "running": {"ended", "crashed"},
    "ended": set(), "crashed": set(),
}
LEASE_TRANSITIONS = {
    "reserved": {"in_flight", "released"},
    "in_flight": {"settled", "unknown"},
    "unknown": {"settled", "released"}, "settled": set(), "released": set(),
}
REQUEST_TRANSITIONS = {"pending": {"answered", "cancelled", "expired"}, "answered": set(), "cancelled": set(), "expired": set()}
ACTION_TRANSITIONS = {
    "prepared": {"running", "cancelled"},
    "running": {"succeeded", "failed", "unknown"},
    "unknown": {"succeeded", "failed"},
    "succeeded": set(), "failed": set(), "cancelled": set(),
}
GRANT_TRANSITIONS = {"active": {"revoked", "expired"}, "revoked": set(), "expired": set()}

_GRAPHS = {
    "TaskRecord": ("status", TASK_TRANSITIONS),
    "AttemptRecord": ("state", ATTEMPT_TRANSITIONS),
    "RunRecord": ("state", RUN_TRANSITIONS),
    "BudgetLease": ("status", LEASE_TRANSITIONS),
    "HumanRequest": ("status", REQUEST_TRANSITIONS),
    "ActionRecord": ("status", ACTION_TRANSITIONS),
    "ArtifactGrant": ("status", GRANT_TRANSITIONS),
}
_IDENTITY = {
    "TaskRecord": ("task_id", "root_id", "parent_id", "project_id", "creation_key", "budget_scope_id"),
    "AttemptRecord": ("attempt_id", "task_id", "number"),
    "RunRecord": ("run_id", "task_id", "attempt_id", "resumed_from"),
    "BudgetScope": ("scope_id", "task_id", "root_scope_id", "parent_scope_id", "limits", "active_limit_ms", "expires_at"),
    "BudgetLease": ("lease_id", "scope_id", "task_id", "attempt_id", "run_id", "call_id", "kind", "purpose", "reserved"),
    "HumanRequest": ("request_id", "task_id", "attempt_id", "kind", "prompt", "choices", "requested_from", "action_id", "action_digest", "expires_at", "continuation"),
    "ActionRecord": ("action_id", "task_id", "attempt_id", "tool_call_id", "operation", "effect", "args", "args_hash", "expected_resources", "scope", "constraints_ref", "intent_digest"),
    "ArtifactGrant": ("grant_id", "subject", "artifact_ids", "task_ids", "project_id", "operations", "authority_ref", "expires_at"),
}


def _check(condition: bool, message: str, code: str = "invalid_transition") -> None:
    if not condition:
        raise TransitionError(message, code)


def validate_transition(
    before: MutableRecord,
    after: MutableRecord,
    *,
    expected_revision: int,
    command: str = "transition",
) -> None:
    """Validate a pair of record snapshots, without writing either of them.

    Cross-record prerequisites (report, root state, grants, reply, etc.) require
    ContractSnapshot validation as well. External authorization/atomicity remain
    the responsibility of future services; this function cannot supply them.
    """
    _check(isinstance(before, MutableRecord) and isinstance(after, MutableRecord), "immutable records cannot be updated")
    _check(type(before) is type(after), "record type cannot change")
    # Also validate snapshots produced with Pydantic's unchecked model_copy or
    # model_construct. Do not let these alternate construction APIs bypass us.
    before = type(before).model_validate(before.model_dump(mode="python"))
    after = type(after).model_validate(after.model_dump(mode="python"))
    _check(type(expected_revision) is int and expected_revision == before.revision, "stale expected_revision", "revision_conflict")
    _check(after.revision == before.revision + 1, "revision must increase exactly once", "revision_conflict")
    _check(time_value(after.updated_at) >= time_value(before.updated_at), "updated_at moved backwards")
    _check(command in {"transition", "retry"}, "unknown transition command")
    kind = before.record_type
    _check(kind in _IDENTITY, "unsupported mutable record")
    for name in ("schema_version", "created_at", "record_type") + _IDENTITY[kind]:
        _check(getattr(before, name) == getattr(after, name), f"{name} is immutable")

    if command == "retry":
        _check(kind == "TaskRecord", "retry is only defined for tasks")
        _check(before.status in {"failed", "unverified", "cancelled"} and after.status == "ready", "only a closed unsuccessful task can be explicitly retried")
        _check(after.current_attempt_id is not None and after.current_attempt_id != before.current_attempt_id, "retry requires a new attempt")
        _check(after.latest_report_id is None and not after.output_artifact_refs and after.wait_ref is None and after.reason is None, "retry must clear current result and wait pointers")
        return

    if kind == "BudgetScope":
        _check(after.active_used_ms >= before.active_used_ms, "execution time consumption cannot decrease")
        return

    state_field, graph = _GRAPHS[kind]
    old, new = getattr(before, state_field), getattr(after, state_field)
    _check(bool(graph[old]), f"{old} is closed; no ordinary updates are allowed")
    _check(old == new or new in graph[old], f"{old} -> {new} is not allowed")

    if kind == "TaskRecord":
        if before.current_attempt_id is not None:
            _check(before.current_attempt_id == after.current_attempt_id, "resume/repair must keep the current attempt")
        elif after.current_attempt_id is not None:
            _check(old == "ready" and new == "running", "first attempt is attached on task claim")
        if before.constraints_ref.id == after.constraints_ref.id:
            _check(after.constraints_ref.version >= before.constraints_ref.version, "constraint version cannot move backwards")
    elif kind in {"AttemptRecord", "RunRecord"}:
        if before.started_at is not None:
            _check(after.started_at == before.started_at, "started_at cannot reset on resume")
        if kind == "RunRecord" and old == "prepared" and new == "ended":
            _check(after.started_at is None and after.end_reason in {"startup_failed", "cancelled"}, "unstarted run needs a startup failure/cancellation reason")
    elif kind == "BudgetLease":
        if before.dispatched_at is not None and new != "released":
            _check(after.dispatched_at == before.dispatched_at, "dispatch time cannot change")
        if old == "unknown" and new == "released":
            _check(after.evidence_ref is not None and after.reason is not None, "release of unknown consumption requires reconciliation evidence")
    elif kind in {"HumanRequest", "ArtifactGrant"} and new == "expired":
        _check(after.expires_at is not None and time_value(after.updated_at) >= time_value(after.expires_at), "cannot expire before expires_at")
    elif kind == "ActionRecord" and old != "prepared":
        for name in ("authorization_ref", "approval_request_id", "execution_run_id", "lease_id"):
            _check(getattr(before, name) == getattr(after, name), f"executed action {name} cannot change")


M = TypeVar("M", bound=MutableRecord)


def transition_record(
    record: M,
    state: str | None = None,
    *,
    expected_revision: int,
    at: str,
    command: str = "transition",
    **changes: Any,
) -> M:
    """Return a validated next snapshot. This performs no service transaction."""
    _check(isinstance(record, MutableRecord), "immutable records cannot be updated")
    _check(not {"revision", "updated_at", "status", "state"}.intersection(changes), "reserved transition fields supplied in changes")
    data = record.model_dump(mode="python")
    data.update(changes)
    data.update(revision=record.revision + 1, updated_at=at)
    if state is not None:
        _check(record.record_type in _GRAPHS, "this record has no lifecycle state")
        data[_GRAPHS[record.record_type][0]] = state
    candidate = type(record).model_validate(data)
    validate_transition(record, candidate, expected_revision=expected_revision, command=command)
    return candidate
