"""Immutable, versioned envelopes for recoverable domain facts."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Generic, Mapping, NewType, Optional, Sequence, TypeAlias, TypeVar, cast

from pydantic import JsonValue as _PydanticJsonValue

from mote.contracts.content import ContentDigest

EventId = NewType("EventId", str)
EventType = NewType("EventType", str)
StreamId = NewType("StreamId", str)
CorrelationId = NewType("CorrelationId", str)

JsonScalar: TypeAlias = None | bool | int | float | str
# Pydantic's named recursive alias is used as the runtime/schema spelling, but
# this module remains the sole public owner of the JSON boundary type.
JsonValue: TypeAlias = _PydanticJsonValue

PayloadT = TypeVar("PayloadT", covariant=True)

MAX_METADATA_ENTRIES = 32
MAX_METADATA_BYTES = 16 * 1024
MAX_METADATA_KEY_BYTES = 128
MAX_STREAM_ID_BYTES = 512
MAX_EVENT_TYPE_BYTES = 256

_EVENT_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+$")


def freeze_json(value: object, *, path: str = "value") -> JsonValue:
    """Validate and deeply freeze one JSON value without coercion."""

    if isinstance(value, ContentDigest):
        return str(value)
    if value is None or type(value) in {bool, str}:
        return cast(JsonScalar, value)
    if type(value) is int:
        if not -(2**63) <= value < 2**63:
            raise ValueError(f"{path} integer is outside the signed 64-bit range")
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return value
    if type(value) in {list, tuple}:
        return cast(
            JsonValue,
            tuple(
                freeze_json(item, path=f"{path}[{index}]") for index, item in enumerate(cast(Sequence[object], value))
            ),
        )
    if isinstance(value, Mapping):
        frozen: dict[str, JsonValue] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError(f"{path} contains a non-string object key")
            frozen[key] = freeze_json(item, path=f"{path}.{key}")
        return cast(JsonValue, MappingProxyType(frozen))
    raise TypeError(f"{path} is not JSON-safe: {type(value).__name__}")


def thaw_json(value: JsonValue) -> JsonValue:
    """Return the ordinary JSON representation of a frozen value."""

    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw_json(item) for item in value]
    return value


def _validate_text(value: object, name: str, *, max_bytes: int) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{name} must be a non-empty string")
    if len(value.encode("utf-8")) > max_bytes:
        raise ValueError(f"{name} exceeds its {max_bytes}-byte bound")
    if any(ord(char) < 32 for char in value):
        raise ValueError(f"{name} contains a control character")
    return value


def _validate_datetime(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value


def _freeze_metadata(value: object) -> Mapping[str, JsonValue]:
    frozen = freeze_json(value, path="metadata")
    if not isinstance(frozen, Mapping):
        raise TypeError("metadata must be a JSON object")
    if len(frozen) > MAX_METADATA_ENTRIES:
        raise ValueError(f"metadata exceeds its {MAX_METADATA_ENTRIES}-entry bound")
    for key in frozen:
        if not key or len(key.encode("utf-8")) > MAX_METADATA_KEY_BYTES:
            raise ValueError("metadata contains an invalid or oversized key")
    encoded = json.dumps(
        thaw_json(frozen),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > MAX_METADATA_BYTES:
        raise ValueError(f"metadata exceeds its {MAX_METADATA_BYTES}-byte bound")
    return frozen


@dataclass(frozen=True)
class EventEnvelope(Generic[PayloadT]):
    """A committed fact with journal-assigned ordering and recording time."""

    event_id: EventId
    event_type: EventType
    schema_version: int
    stream_id: StreamId
    sequence: int
    occurred_at: datetime
    recorded_at: datetime
    payload: PayloadT
    session_id: Optional[str] = None
    run_id: Optional[str] = None
    turn_id: Optional[str] = None
    correlation_id: Optional[CorrelationId] = None
    causation_id: Optional[EventId] = None
    trace_id: Optional[str] = None
    span_id: Optional[str] = None
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_text(self.event_id, "event_id", max_bytes=256)
        event_type = _validate_text(self.event_type, "event_type", max_bytes=MAX_EVENT_TYPE_BYTES)
        if _EVENT_TYPE_PATTERN.fullmatch(event_type) is None:
            raise ValueError("event_type must be a stable, namespaced domain name")
        _validate_text(self.stream_id, "stream_id", max_bytes=MAX_STREAM_ID_BYTES)
        if type(self.schema_version) is not int or self.schema_version < 1:
            raise ValueError("schema_version must be a positive integer")
        if type(self.sequence) is not int or self.sequence < 1:
            raise ValueError("sequence must be a positive integer")
        _validate_datetime(self.occurred_at, "occurred_at")
        _validate_datetime(self.recorded_at, "recorded_at")
        object.__setattr__(
            self,
            "payload",
            cast(PayloadT, freeze_json(self.payload, path="payload")),
        )
        object.__setattr__(self, "metadata", _freeze_metadata(self.metadata))
        for name in (
            "session_id",
            "run_id",
            "turn_id",
            "correlation_id",
            "causation_id",
            "trace_id",
            "span_id",
        ):
            value = getattr(self, name)
            if value is not None:
                _validate_text(value, name, max_bytes=256)


__all__ = [
    "CorrelationId",
    "EventEnvelope",
    "EventId",
    "EventType",
    "JsonScalar",
    "JsonValue",
    "MAX_EVENT_TYPE_BYTES",
    "MAX_METADATA_BYTES",
    "MAX_METADATA_ENTRIES",
    "MAX_METADATA_KEY_BYTES",
    "MAX_STREAM_ID_BYTES",
    "StreamId",
    "freeze_json",
    "thaw_json",
]
