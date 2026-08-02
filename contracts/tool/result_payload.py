"""Canonical durable payload variants for tool results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

from mote.contracts.artifact import ArtifactRef
from mote.contracts.events.envelope import JsonValue, freeze_json, thaw_json

MAX_INLINE_BINARY_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class JsonToolPayload:
    value: JsonValue
    kind: Literal["json"] = "json"

    def __post_init__(self) -> None:
        if self.kind != "json":
            raise ValueError("JSON tool payload kind is invalid")
        object.__setattr__(
            self,
            "value",
            freeze_json(self.value, path="tool_result.payload"),
        )

    @classmethod
    def from_value(cls, value: object) -> "JsonToolPayload":
        return cls(freeze_json(value, path="tool_result.payload"))

    def materialize(self) -> object:
        return thaw_json(self.value)


@dataclass(frozen=True, slots=True)
class InlineBinaryToolPayload:
    value: bytes
    kind: Literal["inline_binary"] = "inline_binary"

    def __post_init__(self) -> None:
        if self.kind != "inline_binary":
            raise ValueError("inline binary tool payload kind is invalid")
        if type(self.value) is not bytes:
            raise TypeError("inline binary tool payload must be bytes")
        if not self.value:
            raise ValueError("inline binary tool payload must not be empty")
        if len(self.value) > MAX_INLINE_BINARY_BYTES:
            raise ValueError("inline binary tool payload exceeds the 64 KiB bound; publish an ArtifactRef")

    def materialize(self) -> bytes:
        return self.value


@dataclass(frozen=True, slots=True)
class ArtifactToolPayload:
    reference: ArtifactRef
    kind: Literal["artifact"] = "artifact"

    def __post_init__(self) -> None:
        if self.kind != "artifact":
            raise ValueError("artifact tool payload kind is invalid")
        if not isinstance(self.reference, ArtifactRef):
            raise TypeError("artifact tool payload requires an ArtifactRef")

    def materialize(self) -> ArtifactRef:
        return self.reference


ToolPayload: TypeAlias = JsonToolPayload | InlineBinaryToolPayload | ArtifactToolPayload


def json_tool_payload(value: object) -> JsonToolPayload:
    return JsonToolPayload.from_value(value)


def binary_tool_payload(value: bytes) -> InlineBinaryToolPayload:
    return InlineBinaryToolPayload(value)


__all__ = [
    "ArtifactToolPayload",
    "InlineBinaryToolPayload",
    "JsonToolPayload",
    "MAX_INLINE_BINARY_BYTES",
    "ToolPayload",
    "binary_tool_payload",
    "json_tool_payload",
]
