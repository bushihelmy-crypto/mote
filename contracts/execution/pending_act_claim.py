"""Durable ownership claim for one PendingAct frontier."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from mote.contracts.execution.pending_act_identity import PendingActFrontierId
from mote.contracts.tool.identity import ToolInvocationId


@dataclass(frozen=True, slots=True)
class PendingActClaimId:
    value: str

    def __post_init__(self) -> None:
        if type(self.value) is not str or not self.value:
            raise ValueError("PendingActClaimId must be non-empty")


@dataclass(frozen=True, slots=True)
class PendingActExecutionClaim:
    claim_id: PendingActClaimId
    frontier_id: PendingActFrontierId
    owner_id: str
    incarnation_id: str
    claim_revision: int
    fencing_token: int
    acquired_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.claim_id, PendingActClaimId):
            raise TypeError("claim_id has the wrong type")
        if not isinstance(self.frontier_id, PendingActFrontierId):
            raise TypeError("frontier_id has the wrong type")
        if not self.owner_id or not self.incarnation_id:
            raise ValueError("claim owner identities must be non-empty")
        if type(self.claim_revision) is not int or self.claim_revision < 0:
            raise ValueError("claim revision must be non-negative")
        if type(self.fencing_token) is not int or self.fencing_token < 1:
            raise ValueError("claim fencing token must be positive")
        if self.acquired_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("claim instants must be timezone-aware")
        if self.expires_at <= self.acquired_at:
            raise ValueError("claim expiry must follow acquisition")


@dataclass(frozen=True, slots=True)
class PendingActInvokePermit:
    claim_id: PendingActClaimId
    frontier_id: PendingActFrontierId
    owner_id: str
    incarnation_id: str
    claim_revision: int
    fencing_token: int
    frontier_revision: int
    invocation_id: ToolInvocationId
    fileops_transaction_id: str | None = None

    def __post_init__(self) -> None:
        if self.fileops_transaction_id is not None and (
            type(self.fileops_transaction_id) is not str or not self.fileops_transaction_id
        ):
            raise ValueError("invoke permit file transaction id must be non-empty")


__all__ = [
    "PendingActClaimId",
    "PendingActExecutionClaim",
    "PendingActInvokePermit",
]
