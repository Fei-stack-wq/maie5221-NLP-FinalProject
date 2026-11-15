"""Strict, serializable value types for the opt-in M0-01 contracts.

These are data snapshots, not persistence, authorization or execution services.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from typing import Annotated, Any, Literal, TypeVar, Union

from pydantic import (
    AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field,
    PlainSerializer, StrictBool, StrictInt, StrictStr, model_validator,
)


def non_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


Id = Annotated[StrictStr, AfterValidator(non_blank)]
Text = Id
UInt = Annotated[StrictInt, Field(ge=0)]
PositiveInt = Annotated[StrictInt, Field(gt=0)]


def _timestamp(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)", value):
        raise ValueError("expected a UTC RFC 3339 timestamp (microsecond precision or less)")
    datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.removesuffix("+00:00") + "Z" if value.endswith("+00:00") else value


Timestamp = Annotated[StrictStr, AfterValidator(_timestamp)]


def time_value(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _hash(value: str) -> str:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ValueError("expected sha256 followed by 64 lowercase hexadecimal digits")
    return value


Hash = Annotated[StrictStr, AfterValidator(_hash)]
T = TypeVar("T")


def _array(value: Any) -> tuple:
    if not isinstance(value, (list, tuple)):
        raise ValueError("expected an array")
    return tuple(value)


Array = Annotated[tuple[T, ...], BeforeValidator(_array)]


class FrozenDict(dict):
    """Read-only JSON mapping, including nested values; not a security boundary."""
    def _deny(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("contract JSON values are immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _deny

    def __copy__(self) -> FrozenDict:
        return self

    def __deepcopy__(self, memo: dict) -> FrozenDict:
        return self


def freeze_json(value: Any) -> Any:
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, dict) and all(type(k) is str for k in value):
        return FrozenDict({k: freeze_json(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(v) for v in value)
    raise ValueError("expected finite JSON values with string object keys")


def thaw_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: thaw_json(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [thaw_json(v) for v in value]
    return value


def _json_object(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return freeze_json(value)


JsonObject = Annotated[
    dict[str, Any], BeforeValidator(_json_object), AfterValidator(freeze_json),
    PlainSerializer(thaw_json, return_type=dict),
]


def unique(values: Any, label: str, *, key: Any = None) -> None:
    keys = list(values) if key is None else [key(v) for v in values]
    if len(keys) != len(set(keys)):
        raise ValueError(f"{label} must not contain duplicate identifiers")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


class ContractModel(BaseModel):
    model_config = ConfigDict(
        strict=True, extra="forbid", frozen=True, revalidate_instances="always",
        validate_default=True,
    )


class Record(ContractModel):
    schema_version: PositiveInt = 1
    created_at: Timestamp

    @model_validator(mode="after")
    def supported_schema(self) -> Record:
        require(self.schema_version == 1, "unsupported schema_version")
        return self


class MutableRecord(Record):
    """A snapshot of a logically mutable record; updates create a new snapshot."""
    revision: PositiveInt
    updated_at: Timestamp

    @model_validator(mode="after")
    def revision_time(self) -> MutableRecord:
        require(time_value(self.updated_at) >= time_value(self.created_at), "updated_at precedes created_at")
        return self


class VersionRef(ContractModel):
    id: Id
    version: PositiveInt


class ActorRef(ContractModel):
    kind: Literal["user", "profile", "service"]
    id: Id


class ArtifactRef(ContractModel):
    artifact_id: Id
    content_hash: Hash


class EvidenceRef(ContractModel):
    kind: Literal["artifact", "receipt", "reply", "catalog"]
    id: Id


class ScopeRef(ContractModel):
    project_id: Id
    root_task_id: Id


class PresentResource(ContractModel):
    resource_id: Id
    state: Literal["present"]
    version: Hash


class AbsentResource(ContractModel):
    resource_id: Id
    state: Literal["absent"]


ResourceVersion = Annotated[Union[PresentResource, AbsentResource], Field(discriminator="state")]


def canonical_bytes(value: Any) -> bytes:
    """Documented v1 hash encoding: no floats, including nested parameters."""
    value = thaw_json(freeze_json(value))

    def check(v: Any) -> None:
        if type(v) is float:
            raise ValueError("canonical action parameters do not allow floats")
        if isinstance(v, dict):
            for child in v.values():
                check(child)
        elif isinstance(v, list):
            for child in v:
                check(child)

    check(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def canonical_hash(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))
