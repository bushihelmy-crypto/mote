"""Strict codec for frozen Workflow governance cancellation requests."""

from __future__ import annotations

from mote.contracts.agent.runtime_identity import AgentId, CancellationEpoch, LineageRevision
from mote.contracts.workflow.authority import WorkflowCreateAdmissionId
from mote.contracts.workflow.governance import (
    MAX_WORKFLOW_GOVERNANCE_TARGETS,
    WorkflowGovernanceCancelReason,
    WorkflowGovernanceCancelRequest,
    WorkflowGovernanceScopeCancelRequestId,
)

WORKFLOW_GOVERNANCE_CANCEL_SCHEMA = "mote.workflow-governance-cancel/v1"


def encode_workflow_governance_cancel(value: WorkflowGovernanceCancelRequest) -> dict[str, object]:
    return {
        "schema": WORKFLOW_GOVERNANCE_CANCEL_SCHEMA,
        "request_id": str(value.request_id),
        "root_agent_id": str(value.root_agent_id),
        "subtree_agent_id": str(value.subtree_agent_id),
        "lineage_snapshot_revision": int(value.lineage_snapshot_revision),
        "cancellation_epoch": int(value.cancellation_epoch),
        "target_agent_ids": [str(item) for item in value.target_agent_ids],
        "admitted_workflow_create_ids": [str(item) for item in value.admitted_workflow_create_ids],
        "reason": value.reason.value,
    }


def decode_workflow_governance_cancel(raw: object) -> WorkflowGovernanceCancelRequest:
    fields = {
        "schema",
        "request_id",
        "root_agent_id",
        "subtree_agent_id",
        "lineage_snapshot_revision",
        "cancellation_epoch",
        "target_agent_ids",
        "admitted_workflow_create_ids",
        "reason",
    }
    if type(raw) is not dict or set(raw) != fields:
        raise ValueError("Workflow governance cancel shape is invalid")
    assert isinstance(raw, dict)
    if raw["schema"] != WORKFLOW_GOVERNANCE_CANCEL_SCHEMA:
        raise ValueError("Workflow governance cancel schema is unsupported")
    for name in ("request_id", "root_agent_id", "subtree_agent_id", "reason"):
        if type(raw[name]) is not str or not raw[name]:
            raise TypeError("Workflow governance cancel string field is invalid")
    for name in ("lineage_snapshot_revision", "cancellation_epoch"):
        if type(raw[name]) is not int:
            raise TypeError("Workflow governance cancel generation is invalid")
    targets = raw["target_agent_ids"]
    admissions = raw["admitted_workflow_create_ids"]
    if type(targets) is not list or type(admissions) is not list:
        raise TypeError("Workflow governance frozen targets must be lists")
    if len(targets) > MAX_WORKFLOW_GOVERNANCE_TARGETS or len(admissions) > MAX_WORKFLOW_GOVERNANCE_TARGETS:
        raise ValueError("Workflow governance frozen target cap exceeded")
    if any(type(item) is not str or not item for item in (*targets, *admissions)):
        raise TypeError("Workflow governance frozen identity is invalid")
    return WorkflowGovernanceCancelRequest(
        WorkflowGovernanceScopeCancelRequestId(raw["request_id"]),
        AgentId(raw["root_agent_id"]),
        AgentId(raw["subtree_agent_id"]),
        LineageRevision(raw["lineage_snapshot_revision"]),
        CancellationEpoch(raw["cancellation_epoch"]),
        tuple(AgentId(item) for item in targets),
        tuple(WorkflowCreateAdmissionId(item) for item in admissions),
        WorkflowGovernanceCancelReason(raw["reason"]),
    )


__all__ = [
    "WORKFLOW_GOVERNANCE_CANCEL_SCHEMA",
    "decode_workflow_governance_cancel",
    "encode_workflow_governance_cancel",
]
