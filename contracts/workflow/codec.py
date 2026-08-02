"""Strict codecs for durable Workflow terminal facts."""

from __future__ import annotations

from collections.abc import Mapping

from mote.contracts.agent.runtime_identity import AgentId, CancellationEpoch, IncarnationGeneration, LineageRevision
from mote.contracts.artifact import ArtifactRef, ArtifactRetention, ArtifactSensitivity
from mote.contracts.clock import AbsoluteInstant
from mote.contracts.session.identity import SessionId
from mote.contracts.workflow.authority import (
    WorkflowCreateAdmissionId,
    WorkflowRunAccessGrant,
    WorkflowRunCreationProvenance,
)
from mote.contracts.workflow.identity import WorkflowRunId
from mote.contracts.workflow.result import (
    WorkflowCancelled,
    WorkflowFailed,
    WorkflowSucceededArtifact,
    WorkflowSucceededInline,
    WorkflowTerminalResult,
    WorkflowTimedOut,
)

WORKFLOW_TERMINAL_RESULT_SCHEMA = "mote.workflow-terminal-result/v1"


def encode_workflow_terminal_result(result: WorkflowTerminalResult) -> dict[str, object]:
    outcome = result.outcome
    if isinstance(outcome, WorkflowSucceededInline):
        kind, payload = "success_inline", {"content": outcome.content}
    elif isinstance(outcome, WorkflowSucceededArtifact):
        artifact = outcome.artifact
        kind, payload = "success_artifact", {
            "artifact_id": artifact.artifact_id,
            "revision": artifact.revision,
            "representation": artifact.representation,
            "artifact_kind": artifact.kind,
            "mime_type": artifact.mime_type,
            "content_ref": artifact.content_ref,
            "digest": artifact.digest,
            "size": artifact.size,
            "retention": artifact.retention.value,
            "sensitivity": artifact.sensitivity.value,
            "suggested_name": artifact.suggested_name,
        }
    elif isinstance(outcome, WorkflowFailed):
        kind, payload = "failure", {"code": outcome.code, "message": outcome.message}
    elif isinstance(outcome, WorkflowCancelled):
        kind, payload = "cancelled", {"reason": outcome.reason}
    elif isinstance(outcome, WorkflowTimedOut):
        kind, payload = "timed_out", {"reason": outcome.reason}
    else:
        raise TypeError("unsupported Workflow terminal result variant")
    return {
        "schema": WORKFLOW_TERMINAL_RESULT_SCHEMA,
        "run_id": str(result.run_id),
        "terminal_revision": result.terminal_revision,
        "kind": kind,
        "payload": payload,
    }


def decode_workflow_terminal_result(raw: Mapping[str, object]) -> WorkflowTerminalResult:
    fields = {"schema", "run_id", "terminal_revision", "kind", "payload"}
    if type(raw) is not dict or set(raw) != fields:
        raise ValueError("Workflow terminal result shape is invalid")
    if raw["schema"] != WORKFLOW_TERMINAL_RESULT_SCHEMA:
        raise ValueError("Workflow terminal result schema is unsupported")
    if type(raw["run_id"]) is not str or not raw["run_id"]:
        raise TypeError("Workflow terminal run identity is invalid")
    if type(raw["terminal_revision"]) is not int or raw["terminal_revision"] < 1:
        raise TypeError("Workflow terminal revision is invalid")
    if type(raw["kind"]) is not str or type(raw["payload"]) is not dict:
        raise TypeError("Workflow terminal discriminator or payload is invalid")
    payload = raw["payload"]
    kind = raw["kind"]
    if kind == "success_inline":
        _require_strings(payload, {"content"}, allow_empty=True)
        outcome = WorkflowSucceededInline(payload["content"])
    elif kind == "success_artifact":
        artifact_fields = {
            "artifact_id",
            "revision",
            "representation",
            "artifact_kind",
            "mime_type",
            "content_ref",
            "digest",
            "size",
            "retention",
            "sensitivity",
            "suggested_name",
        }
        if set(payload) != artifact_fields:
            raise ValueError("Workflow terminal artifact shape is invalid")
        string_fields = artifact_fields - {"revision", "size"}
        if any(type(payload[name]) is not str for name in string_fields):
            raise TypeError("Workflow terminal artifact string field is invalid")
        if type(payload["revision"]) is not int or type(payload["size"]) is not int:
            raise TypeError("Workflow terminal artifact numeric field is invalid")
        outcome = WorkflowSucceededArtifact(
            ArtifactRef(
                artifact_id=payload["artifact_id"],
                revision=payload["revision"],
                representation=payload["representation"],
                kind=payload["artifact_kind"],
                mime_type=payload["mime_type"],
                content_ref=payload["content_ref"],
                digest=payload["digest"],
                size=payload["size"],
                retention=ArtifactRetention(payload["retention"]),
                sensitivity=ArtifactSensitivity(payload["sensitivity"]),
                suggested_name=payload["suggested_name"],
            )
        )
    elif kind == "failure":
        _require_strings(payload, {"code", "message"})
        outcome = WorkflowFailed(payload["code"], payload["message"])
    elif kind == "cancelled":
        _require_strings(payload, {"reason"})
        outcome = WorkflowCancelled(payload["reason"])
    elif kind == "timed_out":
        _require_strings(payload, {"reason"})
        outcome = WorkflowTimedOut(payload["reason"])
    else:
        raise ValueError("Workflow terminal result kind is unsupported")
    return WorkflowTerminalResult(WorkflowRunId(raw["run_id"]), raw["terminal_revision"], outcome)


def _require_strings(payload: dict[object, object], fields: set[str], *, allow_empty: bool = False) -> None:
    if set(payload) != fields:
        raise ValueError("Workflow terminal variant shape is invalid")
    if any(type(payload[field]) is not str or (not allow_empty and not payload[field]) for field in fields):
        raise TypeError("Workflow terminal variant string field is invalid")


def encode_workflow_provenance(value: WorkflowRunCreationProvenance) -> dict[str, object]:
    return {
        "workflow_create_admission_id": str(value.workflow_create_admission_id),
        "creator_logical_agent_id": str(value.creator_logical_agent_id),
        "creator_incarnation_generation": int(value.creator_incarnation_generation),
        "creator_lineage_revision": int(value.creator_lineage_revision),
        "creator_cancellation_epoch": int(value.creator_cancellation_epoch),
        "creator_session_id": str(value.creator_session_id),
        "root_governance_agent_id": str(value.root_governance_agent_id),
        "created_at": value.created_at.to_dict(),
    }


def decode_workflow_provenance(raw: object) -> WorkflowRunCreationProvenance:
    fields = {
        "workflow_create_admission_id",
        "creator_logical_agent_id",
        "creator_incarnation_generation",
        "creator_lineage_revision",
        "creator_cancellation_epoch",
        "creator_session_id",
        "root_governance_agent_id",
        "created_at",
    }
    if type(raw) is not dict or set(raw) != fields:
        raise ValueError("Workflow provenance shape is invalid")
    assert isinstance(raw, dict)
    string_fields = fields - {
        "creator_incarnation_generation",
        "creator_lineage_revision",
        "creator_cancellation_epoch",
        "created_at",
    }
    if any(type(raw[field]) is not str or not raw[field] for field in string_fields):
        raise TypeError("Workflow provenance identity is invalid")
    for field in ("creator_incarnation_generation", "creator_lineage_revision", "creator_cancellation_epoch"):
        if type(raw[field]) is not int:
            raise TypeError("Workflow provenance generation is invalid")
    return WorkflowRunCreationProvenance(
        WorkflowCreateAdmissionId(raw["workflow_create_admission_id"]),
        AgentId(raw["creator_logical_agent_id"]),
        IncarnationGeneration(raw["creator_incarnation_generation"]),
        LineageRevision(raw["creator_lineage_revision"]),
        CancellationEpoch(raw["creator_cancellation_epoch"]),
        SessionId(raw["creator_session_id"]),
        AgentId(raw["root_governance_agent_id"]),
        AbsoluteInstant.from_dict(raw["created_at"]),
    )


def encode_workflow_access_grant(value: WorkflowRunAccessGrant) -> dict[str, str]:
    return {
        "authorized_logical_agent_id": str(value.authorized_logical_agent_id),
        "root_governance_agent_id": str(value.root_governance_agent_id),
    }


def decode_workflow_access_grant(raw: object) -> WorkflowRunAccessGrant:
    fields = {"authorized_logical_agent_id", "root_governance_agent_id"}
    if type(raw) is not dict or set(raw) != fields:
        raise ValueError("Workflow access grant shape is invalid")
    assert isinstance(raw, dict)
    if any(type(raw[field]) is not str or not raw[field] for field in fields):
        raise TypeError("Workflow access grant identity is invalid")
    return WorkflowRunAccessGrant(
        AgentId(raw["authorized_logical_agent_id"]),
        AgentId(raw["root_governance_agent_id"]),
    )


__all__ = [
    "WORKFLOW_TERMINAL_RESULT_SCHEMA",
    "decode_workflow_terminal_result",
    "encode_workflow_terminal_result",
    "decode_workflow_access_grant",
    "decode_workflow_provenance",
    "encode_workflow_access_grant",
    "encode_workflow_provenance",
]
