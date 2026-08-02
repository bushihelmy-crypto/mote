"""Immutable creation and access facts for durable Workflow runs."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum

from mote.contracts.agent.runtime_identity import AgentId, CancellationEpoch, IncarnationGeneration, LineageRevision
from mote.contracts.clock import AbsoluteInstant
from mote.contracts.session.identity import SessionId
from mote.contracts.workflow.identity import WorkflowDefinitionId


class WorkflowCreateAdmissionId(str):
    def __new__(cls, value: str) -> "WorkflowCreateAdmissionId":
        if type(value) is not str or not value:
            raise ValueError("WorkflowCreateAdmissionId must be non-empty")
        return str.__new__(cls, value)

    @staticmethod
    def derive(
        logical_agent_id: AgentId,
        create_request_id: str,
        definition_id: WorkflowDefinitionId,
    ) -> "WorkflowCreateAdmissionId":
        if not create_request_id:
            raise ValueError("Workflow create request identity must be non-empty")
        material = f"{logical_agent_id}\0{create_request_id}\0{definition_id}".encode()
        return WorkflowCreateAdmissionId("wfca_" + hashlib.sha256(material).hexdigest())


@dataclass(frozen=True, slots=True)
class WorkflowRunCreationProvenance:
    workflow_create_admission_id: WorkflowCreateAdmissionId
    creator_logical_agent_id: AgentId
    creator_incarnation_generation: IncarnationGeneration
    creator_lineage_revision: LineageRevision
    creator_cancellation_epoch: CancellationEpoch
    creator_session_id: SessionId
    root_governance_agent_id: AgentId
    created_at: AbsoluteInstant


@dataclass(frozen=True, slots=True)
class WorkflowRunAccessGrant:
    authorized_logical_agent_id: AgentId
    root_governance_agent_id: AgentId


@dataclass(frozen=True, slots=True)
class WorkflowCallerContext:
    logical_agent_id: AgentId
    root_governance_agent_id: AgentId
    incarnation_generation: IncarnationGeneration
    lineage_revision: LineageRevision
    cancellation_epoch: CancellationEpoch
    owner_fencing_token: int

    def __post_init__(self) -> None:
        if type(self.owner_fencing_token) is not int or self.owner_fencing_token < 1:
            raise ValueError("Workflow caller fence must be positive")


class WorkflowCallerAuthorizationDisposition(str, Enum):
    AUTHORIZED = "authorized"
    NOT_FOUND = "not_found"
    NOT_ACTIVE = "not_active"
    INCARNATION_MISMATCH = "incarnation_mismatch"
    ROOT_MISMATCH = "root_mismatch"
    STALE_FENCE = "stale_fence"


@dataclass(frozen=True, slots=True)
class WorkflowCallerAuthorizationReceipt:
    disposition: WorkflowCallerAuthorizationDisposition
    lineage_revision: LineageRevision | None


__all__ = [
    "WorkflowCallerContext",
    "WorkflowCallerAuthorizationDisposition",
    "WorkflowCallerAuthorizationReceipt",
    "WorkflowCreateAdmissionId",
    "WorkflowRunAccessGrant",
    "WorkflowRunCreationProvenance",
]
