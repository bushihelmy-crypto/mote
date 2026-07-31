"""Structured human approval request and response vocabulary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from mote.contracts.authorization import RiskLevel

ApprovalChoice = Literal["allow_once", "allow_session", "deny"]
ApprovalKind = Literal["approval", "escalation"]
ApprovalReasonCode = Literal["ask_rule", "default", "tool", "sandbox"]


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


__all__ = ["ApprovalChoice", "ApprovalKind", "ApprovalReasonCode", "ApprovalRequest"]
