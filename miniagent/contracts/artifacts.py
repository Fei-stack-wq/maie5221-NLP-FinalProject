"""Fixed artifact versions and revocable, exact-set access-grant snapshots.

Nothing here reads files, publishes content, or grants access on its own.
"""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import Field, model_validator

from .base import (
    ActorRef, Array, ArtifactRef, ContractModel, EvidenceRef, Hash, Id,
    MutableRecord, Record, ScopeRef, Text, Timestamp, UInt,
    require, sha256_bytes, time_value, unique,
)


class TaskCreatorRef(ContractModel):
    kind: Literal["task"]
    task_id: Id
    attempt_id: Id
    action_id: Id | None


class ImportCreatorRef(ContractModel):
    kind: Literal["import"]
    actor: ActorRef
    source_id: Id


CreatorRef = Annotated[Union[TaskCreatorRef, ImportCreatorRef], Field(discriminator="kind")]


class ArtifactRecord(Record):
    record_type: Literal["ArtifactRecord"] = "ArtifactRecord"
    artifact_id: Id
    logical_name: Text
    role: Text
    media_type: Text
    size_bytes: UInt
    content_hash: Hash
    storage_key: Text
    creator_ref: CreatorRef
    input_refs: Array[ArtifactRef]
    source_refs: Array[EvidenceRef]
    scope: ScopeRef

    @model_validator(mode="after")
    def valid_provenance(self) -> ArtifactRecord:
        unique(self.input_refs, "input_refs", key=lambda value: value.artifact_id)
        unique(self.source_refs, "source_refs", key=lambda value: (value.kind, value.id))
        require(
            all(value.artifact_id != self.artifact_id for value in self.input_refs),
            "an artifact cannot be its own input",
        )
        return self


class ArtifactGrant(MutableRecord):
    record_type: Literal["ArtifactGrant"] = "ArtifactGrant"
    grant_id: Id
    subject: ActorRef
    artifact_ids: Array[Id]
    task_ids: Array[Id]
    project_id: Id
    operations: Array[Literal["read", "materialize"]]
    authority_ref: Id
    expires_at: Timestamp | None
    status: Literal["active", "revoked", "expired"]

    @model_validator(mode="after")
    def valid_grant(self) -> ArtifactGrant:
        for label in ("artifact_ids", "task_ids", "operations"):
            values = getattr(self, label)
            require(bool(values), f"{label} must not be empty")
            unique(values, label)
        if self.expires_at is not None:
            require(time_value(self.expires_at) >= time_value(self.created_at), "expires_at precedes grant creation")
        if self.status == "expired":
            require(self.expires_at is not None, "an expired grant requires expires_at")
            require(time_value(self.updated_at) >= time_value(self.expires_at), "grant marked expired before expires_at")
        return self


def validate_artifact_content(record: ArtifactRecord, content: bytes) -> None:
    """Validate supplied raw bytes without performing storage or filesystem I/O."""
    require(type(content) is bytes, "artifact content must be bytes")
    require(len(content) == record.size_bytes, "artifact size_bytes does not match content")
    require(sha256_bytes(content) == record.content_hash, "artifact content_hash does not match content")


__all__ = [
    "ArtifactGrant", "ArtifactRecord", "CreatorRef", "ImportCreatorRef",
    "TaskCreatorRef", "validate_artifact_content",
]
