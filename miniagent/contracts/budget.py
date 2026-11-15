"""Budget snapshots and accounting invariants, without a ledger implementation."""
from __future__ import annotations

from typing import Literal

from pydantic import model_validator

from .base import (
    ContractModel, EvidenceRef, Id, MutableRecord, Text, Timestamp, UInt,
    require, time_value,
)


class Quantity(ContractModel):
    tokens: UInt
    llm_calls: UInt
    tool_calls: UInt


class BudgetScope(MutableRecord):
    record_type: Literal["BudgetScope"] = "BudgetScope"
    scope_id: Id
    task_id: Id
    root_scope_id: Id
    parent_scope_id: Id | None
    limits: Quantity
    active_limit_ms: UInt
    active_used_ms: UInt
    expires_at: Timestamp | None

    @model_validator(mode="after")
    def valid_scope(self) -> BudgetScope:
        require((self.parent_scope_id is None) == (self.root_scope_id == self.scope_id),
                "only the root budget has parent_scope_id=null and root_scope_id=scope_id")
        require(self.parent_scope_id != self.scope_id, "budget scope cannot be its own parent")
        # Actual usage may exceed a limit. Keep that evidence instead of hiding it
        # behind a validation error; the ledger must deny subsequent admission.
        return self


BudgetLeaseStatus = Literal["reserved", "in_flight", "settled", "released", "unknown"]


class BudgetLease(MutableRecord):
    record_type: Literal["BudgetLease"] = "BudgetLease"
    lease_id: Id
    scope_id: Id
    task_id: Id
    attempt_id: Id
    run_id: Id
    call_id: Id
    kind: Literal["llm", "tool"]
    purpose: Text
    reserved: Quantity
    actual: Quantity | None
    status: BudgetLeaseStatus
    dispatched_at: Timestamp | None
    settled_at: Timestamp | None
    evidence_ref: EvidenceRef | None
    reason: Text | None

    @model_validator(mode="after")
    def valid_lease(self) -> BudgetLease:
        counter = "llm_calls" if self.kind == "llm" else "tool_calls"
        require(getattr(self.reserved, counter) >= 1, f"{self.kind} lease must reserve at least one {counter}")
        for name in ("dispatched_at", "settled_at"):
            value = getattr(self, name)
            if value is not None:
                require(time_value(self.created_at) <= time_value(value) <= time_value(self.updated_at),
                        f"{name} must be between created_at and updated_at")
        if self.dispatched_at is not None and self.settled_at is not None:
            require(time_value(self.settled_at) >= time_value(self.dispatched_at),
                    "settled_at precedes dispatched_at")
        if self.status == "reserved":
            require(self.dispatched_at is None, "reserved lease has not been dispatched")
        if self.status in {"in_flight", "unknown", "settled"}:
            require(self.dispatched_at is not None, "dispatched lease requires dispatched_at")
        if self.status in {"reserved", "in_flight", "unknown"}:
            require(self.actual is None and self.settled_at is None,
                    "unsettled lease cannot claim actual usage or a settlement time")
        else:
            require(self.actual is not None and self.settled_at is not None and self.evidence_ref is not None,
                    "settled or released lease requires actual usage, time and evidence")
        if self.status == "settled":
            require(getattr(self.actual, counter) >= 1,
                    "a dispatched request consumes its call count even with zero tokens")
        if self.status in {"unknown", "released"}:
            require(self.reason is not None, "unknown or released lease requires a reason")
        if self.status == "released":
            require(self.dispatched_at is None, "a confirmed dispatched request cannot be released")
            require(self.actual.tokens == self.actual.llm_calls == self.actual.tool_calls == 0,
                    "released lease must have confirmed zero usage in all dimensions")
        return self
