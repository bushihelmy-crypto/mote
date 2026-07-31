"""Durable owner/daemon reconciliation records for ambiguous executions."""

from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from mote.contracts.inference.base import FrozenContract


class ReconciliationState(StrEnum):
    OPEN = "open"
    AUTO_RECONCILING = "auto_reconciling"
    EVIDENCE_AVAILABLE = "evidence_available"
    OWNER_ACTION_REQUIRED = "owner_action_required"
    OWNER_APPLIED = "owner_applied"
    OWNER_REJECTED = "owner_rejected"


class OwnerDecision(StrEnum):
    APPLY = "apply"
    REJECT = "reject"


class OwnerCommand(FrozenContract):
    schema_version: Literal[1] = 1
    command_id: str = Field(min_length=1, max_length=256)
    proposal_id: str = Field(min_length=1, max_length=256)
    owner_id: str = Field(min_length=1, max_length=256)
    execution_id: str = Field(min_length=1, max_length=256)
    generation_id: str = Field(min_length=1, max_length=256)
    strategy_id: str = Field(min_length=1, max_length=256)
    evidence_digests: tuple[str, ...] = Field(min_length=1)
    issued_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _validate_command(self) -> "OwnerCommand":
        if self.issued_at.utcoffset() is None:
            raise ValueError("owner command timestamp must be timezone-aware")
        if len(set(self.evidence_digests)) != len(self.evidence_digests):
            raise ValueError("owner command evidence digests must be unique")
        return self


class ResolutionProposal(FrozenContract):
    schema_version: Literal[1] = 1
    proposal_id: str = Field(min_length=1, max_length=256)
    owner_id: str = Field(min_length=1, max_length=256)
    execution_id: str = Field(min_length=1, max_length=256)
    generation_id: str = Field(min_length=1, max_length=256)
    strategy_id: str = Field(min_length=1, max_length=256)
    evidence_digests: tuple[str, ...] = Field(min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _validate_evidence(self) -> "ResolutionProposal":
        if self.created_at.utcoffset() is None:
            raise ValueError("proposal timestamp must be timezone-aware")
        if len(set(self.evidence_digests)) != len(self.evidence_digests):
            raise ValueError("proposal evidence digests must be unique")
        if any(
            len(digest) != 71
            or not digest.startswith("sha256:")
            or any(character not in "0123456789abcdef" for character in digest[7:])
            for digest in self.evidence_digests
        ):
            raise ValueError("proposal evidence digest is invalid")
        return self


class OwnerAcknowledgement(FrozenContract):
    schema_version: Literal[1] = 1
    proposal_id: str = Field(min_length=1, max_length=256)
    owner_id: str = Field(min_length=1, max_length=256)
    decision: OwnerDecision
    owner_journal_revision: int = Field(ge=1)
    acknowledged_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _validate_timestamp(self) -> "OwnerAcknowledgement":
        if self.acknowledged_at.utcoffset() is None:
            raise ValueError("acknowledgement timestamp must be timezone-aware")
        return self


__all__ = [
    "OwnerCommand",
    "OwnerAcknowledgement",
    "OwnerDecision",
    "ReconciliationState",
    "ResolutionProposal",
]
