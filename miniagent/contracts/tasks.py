"""Task, attempt and continuation snapshots; no scheduling or persistence."""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import Field, StrictBool, model_validator

from .base import (
    Array, ArtifactRef, ContractModel, Id, JsonObject, MutableRecord,
    PositiveInt, Record, Text, Timestamp, UInt, VersionRef, require,
    time_value, unique,
)


class HumanWaitRef(ContractModel):
    kind: Literal["human"]
    request_id: Id


class ChildrenWaitRef(ContractModel):
    kind: Literal["children"]
    child_task_ids: Array[Id]

    @model_validator(mode="after")
    def valid_children(self) -> ChildrenWaitRef:
        require(bool(self.child_task_ids), "children wait requires at least one child")
        unique(self.child_task_ids, "child_task_ids")
        return self


class ExternalWaitRef(ContractModel):
    kind: Literal["external"]
    action_id: Id


WaitRef = Annotated[
    Union[HumanWaitRef, ChildrenWaitRef, ExternalWaitRef],
    Field(discriminator="kind"),
]
TaskStatus = Literal[
    "created", "ready", "running", "verifying", "waiting_user", "waiting_agent",
    "blocked", "completed", "unverified", "failed", "cancelled",
]


class TaskRecord(MutableRecord):
    record_type: Literal["TaskRecord"] = "TaskRecord"
    task_id: Id
    root_id: Id
    parent_id: Id | None
    creation_key: Text
    objective: Text
    project_id: Id
    owner_profile_id: Id | None
    assignee_profile_id: Id | None
    deps: Array[Id]
    constraints_ref: VersionRef
    acceptance_ref: VersionRef | None
    budget_scope_id: Id
    input_artifact_refs: Array[ArtifactRef]
    output_artifact_refs: Array[ArtifactRef]
    status: TaskStatus
    reason: Text | None
    current_attempt_id: Id | None
    latest_report_id: Id | None
    wait_ref: WaitRef | None

    @model_validator(mode="after")
    def valid_task(self) -> TaskRecord:
        require((self.parent_id is None) == (self.root_id == self.task_id),
                "only the root task has parent_id=null and root_id=task_id")
        require(self.parent_id != self.task_id, "task cannot be its own parent")
        unique(self.deps, "deps")
        require(self.task_id not in self.deps, "task cannot depend on itself")
        for name in ("input_artifact_refs", "output_artifact_refs"):
            unique(getattr(self, name), name, key=lambda ref: ref.artifact_id)
        if self.status in {"ready", "running", "verifying", "waiting_user", "waiting_agent", "completed", "unverified"}:
            require(self.owner_profile_id is not None and self.assignee_profile_id is not None,
                    "ready or executing tasks require an owner and assignee")
        if self.status in {"running", "verifying", "waiting_user", "waiting_agent", "completed", "unverified"}:
            require(self.current_attempt_id is not None, "executed tasks require current_attempt_id")
        if self.status in {"blocked", "failed", "unverified", "cancelled"}:
            require(self.reason is not None, "blocked or unsuccessful tasks require a reason")
        if self.latest_report_id is not None:
            require(self.current_attempt_id is not None, "a report must belong to the current attempt")
        if self.status == "completed":
            require(self.acceptance_ref is not None and self.latest_report_id is not None,
                    "completed requires an acceptance specification and report")
        if self.status == "waiting_user":
            require(isinstance(self.wait_ref, HumanWaitRef), "waiting_user requires a human wait")
        elif self.status == "waiting_agent":
            require(isinstance(self.wait_ref, ChildrenWaitRef), "waiting_agent requires a children wait")
            require(self.task_id not in self.wait_ref.child_task_ids, "task cannot wait on itself")
        elif self.status == "blocked" and self.reason == "external_state_unknown":
            require(isinstance(self.wait_ref, ExternalWaitRef), "unknown external state requires an action wait")
        else:
            require(self.wait_ref is None, "task must clear wait_ref outside its matching wait state")
        return self


AttemptState = Literal["pending", "running", "suspended", "succeeded", "unverified", "failed", "cancelled"]


def _execution_times(record: MutableRecord, started_at: str | None, ended_at: str | None) -> None:
    for label, value in (("started_at", started_at), ("ended_at", ended_at)):
        if value is not None:
            require(time_value(record.created_at) <= time_value(value) <= time_value(record.updated_at),
                    f"{label} must be between created_at and updated_at")
    if started_at is not None and ended_at is not None:
        require(time_value(ended_at) >= time_value(started_at), "ended_at precedes started_at")


class AttemptRecord(MutableRecord):
    record_type: Literal["AttemptRecord"] = "AttemptRecord"
    attempt_id: Id
    task_id: Id
    number: PositiveInt
    state: AttemptState
    context_ref: Id | None
    checkpoint_id: Id | None
    report_ids: Array[Id]
    started_at: Timestamp | None
    ended_at: Timestamp | None
    end_reason: Text | None

    @model_validator(mode="after")
    def valid_attempt(self) -> AttemptRecord:
        unique(self.report_ids, "report_ids")
        _execution_times(self, self.started_at, self.ended_at)
        terminal = self.state in {"succeeded", "unverified", "failed", "cancelled"}
        if terminal:
            require(self.ended_at is not None and self.end_reason is not None,
                    "terminal attempt requires ended_at and end_reason")
        else:
            require(self.ended_at is None and self.end_reason is None,
                    "open attempt cannot have an end time or reason")
        if self.state == "pending":
            require(self.started_at is None and self.checkpoint_id is None and not self.report_ids,
                    "pending attempt cannot contain execution history")
        if self.state in {"running", "suspended", "succeeded", "unverified"}:
            require(self.started_at is not None, "started attempt requires started_at")
        if self.started_at is not None:
            require(self.context_ref is not None, "started attempt requires context_ref")
        else:
            require(self.checkpoint_id is None and not self.report_ids,
                    "unstarted attempt cannot contain a checkpoint or report")
        if self.state == "suspended":
            require(self.checkpoint_id is not None, "suspended attempt requires checkpoint_id")
        if self.state == "succeeded":
            require(bool(self.report_ids), "succeeded attempt requires an acceptance report")
        return self


RunState = Literal["prepared", "running", "ended", "crashed"]


class RunRecord(MutableRecord):
    record_type: Literal["RunRecord"] = "RunRecord"
    run_id: Id
    task_id: Id
    attempt_id: Id
    state: RunState
    started_at: Timestamp | None
    ended_at: Timestamp | None
    end_reason: Text | None
    resumed_from: Id | None

    @model_validator(mode="after")
    def valid_run(self) -> RunRecord:
        _execution_times(self, self.started_at, self.ended_at)
        if self.state in {"ended", "crashed"}:
            require(self.ended_at is not None and self.end_reason is not None,
                    "closed run requires ended_at and end_reason")
        else:
            require(self.ended_at is None and self.end_reason is None,
                    "open run cannot have an end time or reason")
        if self.state == "prepared":
            require(self.started_at is None, "prepared run has not started")
        if self.state in {"running", "crashed"}:
            require(self.started_at is not None, "running or crashed run requires started_at")
        if self.state == "ended" and self.started_at is None:
            require(self.end_reason in {"startup_failed", "cancelled"},
                    "unstarted run may only end with startup_failed or cancelled")
        return self


class ResumeCursor(ContractModel):
    step: UInt
    phase: Literal["before_model", "before_action", "after_action", "verifying"]
    pending_action_ids: Array[Id]
    pending_tool_call_ids: Array[Id]
    observed_tool_call_ids: Array[Id]

    @model_validator(mode="after")
    def valid_cursor(self) -> ResumeCursor:
        for name in ("pending_action_ids", "pending_tool_call_ids", "observed_tool_call_ids"):
            unique(getattr(self, name), name)
        require(not set(self.pending_tool_call_ids).intersection(self.observed_tool_call_ids),
                "tool calls cannot be both pending and observed")
        if self.phase == "before_model":
            require(not self.pending_action_ids and not self.pending_tool_call_ids,
                    "before_model cannot skip pending actions or tool results")
        if self.phase == "before_action":
            require(bool(self.pending_action_ids or self.pending_tool_call_ids),
                    "before_action requires a pending action or tool call")
        return self


class PendingDelegation(ContractModel):
    child_task_id: Id
    tool_call_id: Id | None
    delivery_key: Id
    delivered: StrictBool


class Checkpoint(Record):
    record_type: Literal["Checkpoint"] = "Checkpoint"
    checkpoint_id: Id
    task_id: Id
    attempt_id: Id
    run_id: Id
    context_ref: Id
    history_ref: Id
    resume_cursor: ResumeCursor
    constraints_ref: VersionRef
    budget_scope_id: Id
    acceptance_failures: UInt
    loop_state: JsonObject
    applied_message_ids: Array[Id]
    wait_ref: WaitRef | None
    pending_delegations: Array[PendingDelegation]
    completed_action_ids: Array[Id]

    @model_validator(mode="after")
    def valid_checkpoint(self) -> Checkpoint:
        unique(self.applied_message_ids, "applied_message_ids")
        unique(self.completed_action_ids, "completed_action_ids")
        unique(self.pending_delegations, "pending delegation children", key=lambda item: item.child_task_id)
        unique(self.pending_delegations, "delivery keys", key=lambda item: item.delivery_key)
        unique([item.tool_call_id for item in self.pending_delegations if item.tool_call_id is not None],
               "delegation tool call ids")
        require(all(item.child_task_id != self.task_id for item in self.pending_delegations),
                "task cannot delegate to itself")
        require(not set(self.completed_action_ids).intersection(self.resume_cursor.pending_action_ids),
                "completed actions cannot remain pending")
        if isinstance(self.wait_ref, ChildrenWaitRef):
            require(self.task_id not in self.wait_ref.child_task_ids, "task cannot wait on itself")
            child_ids = {item.child_task_id for item in self.pending_delegations}
            require(set(self.wait_ref.child_task_ids).issubset(child_ids),
                    "waiting children must have pending delegation records")
        return self
