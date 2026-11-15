"""Opt-in M0-01 contracts. No changes to the existing runtime or its schemas.

Models validate individual snapshots; ContractSnapshot checks cross-record
references; transition_record validates a proposed revision without writing it.
None of these APIs authenticate users, perform transactions, or execute tools.
"""
from typing import Annotated, Union

from pydantic import Field, TypeAdapter

from .acceptance import (
    AcceptanceReport, AcceptanceSpec, CheckResult, Criterion, EvaluatorRef,
    Target, validate_report,
)
from .artifacts import ArtifactGrant, ArtifactRecord, validate_artifact_content
from .base import ActorRef, ArtifactRef, EvidenceRef, ScopeRef, VersionRef, canonical_hash
from .budget import BudgetLease, BudgetScope, Quantity
from .human import ActionRecord, HumanReply, HumanRequest, action_intent_digest, validate_approval, validate_reply
from .snapshot import ContractSnapshot
from .tasks import AttemptRecord, Checkpoint, RunRecord, TaskRecord
from .transitions import TransitionError, transition_record, validate_transition

ContractRecord = Annotated[
    Union[
        TaskRecord, AttemptRecord, RunRecord, AcceptanceSpec, AcceptanceReport,
        BudgetScope, BudgetLease, ArtifactRecord, ArtifactGrant, HumanRequest,
        HumanReply, ActionRecord, Checkpoint,
    ],
    Field(discriminator="record_type"),
]
_RECORD_ADAPTER = TypeAdapter(ContractRecord)


def parse_record(data: object) -> ContractRecord:
    """Parse a typed wire record, rejecting unknown tags, fields and versions."""
    return _RECORD_ADAPTER.validate_python(data)


__all__ = [
    "AcceptanceReport", "AcceptanceSpec", "ActionRecord", "ActorRef",
    "ArtifactGrant", "ArtifactRecord", "ArtifactRef", "AttemptRecord",
    "BudgetLease", "BudgetScope", "CheckResult", "Checkpoint", "ContractRecord",
    "ContractSnapshot", "Criterion", "EvaluatorRef", "EvidenceRef", "HumanReply",
    "HumanRequest", "Quantity", "RunRecord", "ScopeRef", "Target", "TaskRecord",
    "TransitionError", "VersionRef", "action_intent_digest", "canonical_hash",
    "parse_record", "transition_record", "validate_approval", "validate_artifact_content",
    "validate_reply", "validate_report", "validate_transition",
]
