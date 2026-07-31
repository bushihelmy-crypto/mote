"""Narrow data contracts owned by command protocol semantics."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from mote.contracts.artifact import ArtifactRef
from mote.contracts.conversation import Message
from mote.contracts.model.turn import ModelTurn
from mote.contracts.tool.result import FileChange, ToolMedia


@dataclass(frozen=True, slots=True)
class DecodeContext:
    valid_tool_names: frozenset[str] = frozenset()
    output_binding: str = "text"


@dataclass(frozen=True, slots=True)
class ToolProjectionContext:
    protocol: str
    protocol_version: str
    capability_fingerprint: str


@dataclass(frozen=True, slots=True)
class HistoryProjectionContext:
    protocol: str
    protocol_version: str


class ProtocolIssueDecision(str, Enum):
    REJECT_TURN = "reject_turn"
    IGNORE_COMMAND = "ignore_command"
    REQUEST_CORRECTION = "request_correction"
    TERMINATE = "terminate"


@dataclass(frozen=True, slots=True)
class ProtocolIssue:
    code: str
    message: str
    decision: ProtocolIssueDecision


@dataclass(frozen=True, slots=True)
class ObservationDiagnostic:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class DecodeResult:
    turn: ModelTurn
    protocol_issues: tuple[ProtocolIssue, ...] = ()
    diagnostics: tuple[ObservationDiagnostic, ...] = ()


@dataclass(slots=True)
class ExecutedCommand:
    action_id: str | None
    name: str
    arguments: dict[str, Any]
    output: str = ""
    success: bool = True
    settled: bool = False
    media: list[ToolMedia] = field(default_factory=list)
    artifacts: list[ArtifactRef] = field(default_factory=list)
    file_changes: list[FileChange] = field(default_factory=list)
    retention: str | None = None
    resource_path: str | None = None
    data: object = None


@dataclass(frozen=True, slots=True)
class HistoryProjection:
    messages: tuple[Message, ...]
    fingerprint: str


__all__ = [
    "DecodeContext",
    "DecodeResult",
    "ExecutedCommand",
    "HistoryProjection",
    "HistoryProjectionContext",
    "ObservationDiagnostic",
    "ProtocolIssue",
    "ProtocolIssueDecision",
    "ToolProjectionContext",
]
