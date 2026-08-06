"""Structured human approval request and response vocabulary."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, Mapping

from mote.contracts.authorization import RiskLevel
from mote.contracts.events.envelope import JsonValue, freeze_json
from mote.contracts.execution.pending_act_identity import PendingActFrontierId
from mote.contracts.interaction.approval_identity import ApprovalRequestId
from mote.contracts.tool.identity import ToolInvocationId


@dataclass(frozen=True, slots=True)
class ApprovalChoice:
    disposition: "ApprovalDisposition"
    arguments: Mapping[str, JsonValue] | None = None

    def __post_init__(self) -> None:
        if self.arguments is not None:
            frozen = freeze_json(dict(self.arguments), path="approval choice arguments")
            if not isinstance(frozen, Mapping):
                raise TypeError("approval choice arguments must be an object")
            object.__setattr__(self, "arguments", frozen)

    @classmethod
    def allow_once(cls, arguments: Mapping[str, JsonValue] | None = None) -> "ApprovalChoice":
        return cls(ApprovalDisposition.ALLOW_ONCE, arguments)

    @classmethod
    def allow_session(cls, arguments: Mapping[str, JsonValue] | None = None) -> "ApprovalChoice":
        return cls(ApprovalDisposition.ALLOW_SESSION, arguments)

    @classmethod
    def reject(cls) -> "ApprovalChoice":
        return cls(ApprovalDisposition.REJECT)


ApprovalKind = Literal["approval", "escalation"]
ApprovalReasonCode = Literal["ask_rule", "default", "tool", "sandbox"]


class ApprovalState(StrEnum):
    NOT_REQUIRED = "not_required"
    WAITING = "waiting"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class ApprovalDisposition(StrEnum):
    ALLOW_ONCE = "allow_once"
    ALLOW_SESSION = "allow_session"
    REJECT = "reject"
    CANCEL = "cancel"


@dataclass(frozen=True, slots=True)
class DurableApprovalDecision:
    request_id: ApprovalRequestId
    disposition: ApprovalDisposition
    arguments: Mapping[str, JsonValue] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, ApprovalRequestId):
            raise TypeError("approval decision request_id has the wrong type")
        if self.arguments is not None:
            frozen = freeze_json(dict(self.arguments), path="approval decision arguments")
            if not isinstance(frozen, Mapping):
                raise TypeError("approval decision arguments must be an object")
            object.__setattr__(self, "arguments", frozen)


@dataclass(frozen=True)
class ApprovalRequest:
    tool_name: str
    kind: ApprovalKind = "approval"
    target: str = ""
    paths: list[str] = field(default_factory=list)
    risk: RiskLevel = "medium"
    reason_code: ApprovalReasonCode = "default"
    reason_detail: str = ""
    suggestion: str = ""
    mutates_fs: bool = False
    request_id: ApprovalRequestId | None = None
    frontier_id: PendingActFrontierId | None = None
    invocation_id: ToolInvocationId | None = None
    arguments_revision: int = 0
    arguments_digest: str = ""
    permission_targets_digest: str = ""
    expected_frontier_revision: int = 0

    def __post_init__(self) -> None:
        if type(self.mutates_fs) is not bool:
            raise TypeError("approval mutates_fs must be a boolean")
        if self.request_id is not None and not isinstance(self.request_id, ApprovalRequestId):
            raise TypeError("approval request_id has the wrong type")
        if self.frontier_id is not None and not isinstance(self.frontier_id, PendingActFrontierId):
            raise TypeError("approval frontier_id has the wrong type")
        if self.invocation_id is not None and not isinstance(self.invocation_id, ToolInvocationId):
            raise TypeError("approval invocation_id has the wrong type")
        if type(self.arguments_revision) is not int or self.arguments_revision < 0:
            raise ValueError("approval arguments revision must be non-negative")
        if type(self.expected_frontier_revision) is not int or self.expected_frontier_revision < 0:
            raise ValueError("approval frontier revision must be non-negative")
        bound = (
            self.request_id,
            self.frontier_id,
            self.invocation_id,
            self.arguments_digest,
            self.permission_targets_digest,
        )
        if any(bound) and not all(bound):
            raise ValueError("durable approval identity must be complete or absent")


__all__ = [
    "ApprovalChoice",
    "DurableApprovalDecision",
    "ApprovalDisposition",
    "ApprovalKind",
    "ApprovalReasonCode",
    "ApprovalRequest",
    "ApprovalRequestId",
    "ApprovalState",
]
