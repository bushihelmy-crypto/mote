"""Structured facts shared by tool results and tool events."""

from __future__ import annotations

from dataclasses import dataclass

from mote.contracts.artifact import ArtifactRef
from mote.contracts.tool.result_payload import (
    ArtifactToolPayload,
    InlineBinaryToolPayload,
    JsonToolPayload,
    ToolPayload,
    binary_tool_payload,
    json_tool_payload,
)


@dataclass(frozen=True, slots=True)
class ToolMedia:
    artifact: ArtifactRef
    kind: str = "image"
    ref: str = ""
    mime: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.artifact, ArtifactRef):
            raise TypeError("tool media requires an ArtifactRef")
        if type(self.kind) is not str or not self.kind:
            raise ValueError("tool media kind must be a non-empty string")
        if type(self.ref) is not str:
            raise ValueError("tool media ref must be a string")
        if self.mime is not None and (type(self.mime) is not str or not self.mime):
            raise ValueError("tool media mime must be a non-empty string or null")


@dataclass(frozen=True, slots=True)
class FileChange:
    path: str = ""
    old: str = ""
    new: str = ""
    transaction_id: str = ""
    post_digest: str = ""

    def __post_init__(self) -> None:
        for name in ("path", "old", "new", "transaction_id", "post_digest"):
            if type(getattr(self, name)) is not str:
                raise ValueError(f"file change {name} must be a string")


__all__ = [
    "ArtifactToolPayload",
    "FileChange",
    "InlineBinaryToolPayload",
    "JsonToolPayload",
    "ToolMedia",
    "ToolPayload",
    "binary_tool_payload",
    "json_tool_payload",
]
