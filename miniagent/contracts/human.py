"""Human replies and action snapshots for the opt-in M0-01 contracts.

These models validate data, not user identity, current permissions or external
side effects. The helpers deliberately do not read a clock, authenticate a user,
execute an action, or consult an authorization service.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import TypeAdapter, model_validator

from .base import (
    ActorRef, Array, ContractModel, EvidenceRef, Hash, Id, JsonObject,
    MutableRecord, Record, ResourceVersion, ScopeRef, Text, Timestamp,
    VersionRef, canonical_hash, require, time_value, unique,
)


ActionEffect = Literal["read", "private_write", "external_write"]
ActionStatus = Literal["prepared", "running", "succeeded", "failed", "unknown", "cancelled"]
_EXECUTED_STATES = frozenset({"running", "succeeded", "failed", "unknown"})
_RESOURCES = TypeAdapter(Array[ResourceVersion])
_EFFECT = TypeAdapter(ActionEffect)
_HASH = TypeAdapter(Hash)
_TEXT = TypeAdapter(Text)


class Continuation(ContractModel):
    kind: Literal["answer_question", "authorize_action"]
    tool_call_id: Id | None


class HumanRequest(MutableRecord):
    record_type: Literal["HumanRequest"] = "HumanRequest"
    request_id: Id
    task_id: Id
    attempt_id: Id
    kind: Literal["question", "approval"]
    prompt: Text
    choices: Array[Text]
    requested_from: ActorRef
    action_id: Id | None
    action_digest: Hash | None
    status: Literal["pending", "answered", "cancelled", "expired"]
    expires_at: Timestamp | None
    reply_id: Id | None
    continuation: Continuation

    @model_validator(mode="after")
    def request_conditions(self) -> HumanRequest:
        require(self.requested_from.kind == "user", "v1 requests must be addressed to a user")
        if self.kind == "question":
            require(self.action_id is None and self.action_digest is None,
                    "a question must not bind an approval action")
            require(self.continuation.kind == "answer_question",
                    "question continuation must answer_question")
        else:
            require(self.action_id is not None and self.action_digest is not None,
                    "an approval request requires action_id and action_digest")
            require(self.continuation.kind == "authorize_action",
                    "approval continuation must authorize_action")

        require((self.reply_id is not None) == (self.status == "answered"),
                "only an answered request has a reply_id, and it must have one")
        if self.expires_at is not None:
            require(time_value(self.expires_at) > time_value(self.created_at),
                    "expires_at must follow request creation")
        if self.status == "expired":
            require(self.expires_at is not None, "expired request requires expires_at")
            require(time_value(self.updated_at) >= time_value(self.expires_at),
                    "request cannot expire before expires_at")
        if self.status == "pending" and self.expires_at is not None:
            require(time_value(self.updated_at) < time_value(self.expires_at),
                    "pending snapshot cannot be updated at or after expiry")
        return self


class HumanReply(Record):
    record_type: Literal["HumanReply"] = "HumanReply"
    reply_id: Id
    request_id: Id
    actor: ActorRef
    kind: Literal["clarification", "approval"]
    answer: Text | None
    decision: Literal["allow", "deny"] | None
    replied_at: Timestamp

    @model_validator(mode="after")
    def reply_conditions(self) -> HumanReply:
        require(self.actor.kind == "user", "v1 replies must originate from a user")
        if self.kind == "clarification":
            require(self.answer is not None and self.decision is None,
                    "clarification requires an answer and no approval decision")
        else:
            require(self.decision is not None, "approval reply requires allow or deny")
        require(time_value(self.replied_at) <= time_value(self.created_at),
                "reply cannot be recorded before replied_at")
        return self


def action_intent_digest(
    *,
    operation: str,
    effect: ActionEffect,
    args_hash: str,
    expected_resources: Sequence[ResourceVersion | Mapping[str, Any]],
    scope: ScopeRef | Mapping[str, Any],
    constraints_ref: VersionRef | Mapping[str, Any],
) -> str:
    """Compute the v1 intent hash, independent of resource-array order.

    This validates the hash inputs' structure, but cannot establish whether the
    declared operation/effect describes a real tool or whether access is allowed.
    """
    resources = _RESOURCES.validate_python(expected_resources)
    unique(resources, "expected_resources", key=lambda item: item.resource_id)
    return canonical_hash({
        "operation": _TEXT.validate_python(operation),
        "effect": _EFFECT.validate_python(effect),
        "args_hash": _HASH.validate_python(args_hash),
        "expected_resources": [
            item.model_dump(mode="json")
            for item in sorted(resources, key=lambda item: item.resource_id)
        ],
        "scope": ScopeRef.model_validate(scope).model_dump(mode="json"),
        "constraints_ref": VersionRef.model_validate(constraints_ref).model_dump(mode="json"),
    })


class ActionRecord(MutableRecord):
    record_type: Literal["ActionRecord"] = "ActionRecord"
    action_id: Id
    task_id: Id
    attempt_id: Id
    tool_call_id: Id | None
    operation: Text
    effect: ActionEffect
    args: JsonObject
    args_hash: Hash
    expected_resources: Array[ResourceVersion]
    scope: ScopeRef
    constraints_ref: VersionRef
    intent_digest: Hash
    authorization_ref: Id | None
    approval_request_id: Id | None
    status: ActionStatus
    execution_run_id: Id | None
    lease_id: Id | None
    receipt_ref: EvidenceRef | None
    reason: Text | None

    @model_validator(mode="after")
    def action_conditions(self) -> ActionRecord:
        unique(self.expected_resources, "expected_resources", key=lambda item: item.resource_id)
        require(self.args_hash == canonical_hash(self.args), "args_hash does not match args")
        require(self.intent_digest == action_intent_digest(
            operation=self.operation, effect=self.effect, args_hash=self.args_hash,
            expected_resources=self.expected_resources, scope=self.scope,
            constraints_ref=self.constraints_ref,
        ), "intent_digest does not match the action intent")

        if self.status in _EXECUTED_STATES:
            require(self.authorization_ref is not None, "executed action requires authorization_ref")
            require(self.execution_run_id is not None, "executed action requires execution_run_id")
            require(self.lease_id is not None, "executed action requires lease_id")
            if self.effect == "external_write":
                require(bool(self.expected_resources),
                        "external write requires expected resource states before execution")
        else:
            require(self.execution_run_id is None and self.lease_id is None,
                    "unexecuted action cannot have an execution run or lease")
        if self.status in {"prepared", "running", "cancelled"}:
            require(self.receipt_ref is None, "action has no execution result in this state")
        if self.receipt_ref is not None:
            require(self.receipt_ref.kind == "receipt", "action receipt_ref must reference a receipt")
        if self.status == "succeeded":
            require(self.receipt_ref is not None, "succeeded action requires a receipt")
        if self.status in {"failed", "unknown", "cancelled"}:
            require(self.reason is not None, "failed, unknown or cancelled action requires reason")
        return self


def validate_reply(request: HumanRequest, reply: HumanReply) -> None:
    """Check a proposed pending-request reply or an answered snapshot's reply.

    A valid deny is still a valid reply. No state is mutated and the actor IDs
    are compared, not authenticated. Expiry is checked against the recorded reply
    event; admitting a live request also needs the service's current clock/state.
    """
    request = HumanRequest.model_validate(request)
    reply = HumanReply.model_validate(reply)
    require(request.status in {"pending", "answered"},
            "cancelled or expired request cannot accept a reply")
    require(reply.request_id == request.request_id, "reply references a different request")
    require(reply.actor == request.requested_from, "reply actor is not requested_from")
    expected_kind = "clarification" if request.kind == "question" else "approval"
    require(reply.kind == expected_kind, "reply kind does not match request kind")
    require(time_value(reply.replied_at) >= time_value(request.created_at),
            "reply precedes the request")
    if request.expires_at is not None:
        require(time_value(reply.replied_at) < time_value(request.expires_at),
                "reply arrived at or after expiry")
    if request.status == "answered":
        require(request.reply_id == reply.reply_id, "answered request references a different reply")
        require(time_value(request.updated_at) >= time_value(reply.created_at),
                "answered request snapshot precedes its recorded reply")


def validate_approval(
    action: ActionRecord, request: HumanRequest, reply: HumanReply,
) -> None:
    """Check the exact answered/allow approval chain for a logical action.

    This is not permission to execute: latest policy, resource versions, budget,
    and authenticated actor checks remain the executing services' responsibility.
    Prepared actions may be checked before execution updates their snapshot.
    """
    action = ActionRecord.model_validate(action)
    request = HumanRequest.model_validate(request)
    reply = HumanReply.model_validate(reply)
    validate_reply(request, reply)
    require(request.kind == "approval" and reply.kind == "approval",
            "action requires an approval request and reply")
    require(request.status == "answered" and reply.decision == "allow",
            "approval requires an answered request with decision allow")
    require(action.approval_request_id == request.request_id,
            "action references a different approval request")
    require(request.action_id == action.action_id, "approval is for a different action")
    require(request.task_id == action.task_id, "approval is for a different task")
    require(request.attempt_id == action.attempt_id, "approval is for a different attempt")
    require(request.action_digest == action.intent_digest, "approval intent digest does not match")
    require(request.continuation.tool_call_id == action.tool_call_id,
            "approval continuation is for a different tool call")
    require(time_value(request.created_at) >= time_value(action.created_at),
            "approval request precedes its action intent")
    if action.status in _EXECUTED_STATES:
        require(time_value(action.updated_at) >= time_value(request.updated_at),
                "executed action snapshot precedes the answered approval")


__all__ = [
    "Continuation", "HumanRequest", "HumanReply", "ActionRecord",
    "action_intent_digest", "validate_reply", "validate_approval",
]
