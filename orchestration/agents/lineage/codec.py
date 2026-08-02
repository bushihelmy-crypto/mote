"""Strict codec for the durable Agent lineage projection."""

from __future__ import annotations

import json
from typing import cast

from mote.contracts.agent.lineage import LineageRecord, SpawnLifecycle, SpawnRequest
from mote.contracts.agent.runtime_identity import AgentId, CancellationEpoch, LineageRevision
from mote.contracts.runtime.operation_ownership import (
    EffectCapability,
    OperationBackend,
    OperationOwnership,
    OperationOwnershipRequest,
)
from mote.contracts.workflow import (
    WorkflowCreateAdmission,
    WorkflowCreateAdmissionId,
    WorkflowCreateAdmissionLifecycle,
    WorkflowDefinitionId,
    WorkflowRunId,
    WorkflowRunReference,
)

LINEAGE_SCHEMA = "mote.agent-lineage/v3"
_TOP = {"schema", "revision", "records", "workflow_create_admissions"}
_RECORD = {
    "request",
    "logical_agent_id",
    "lifecycle",
    "revision",
    "path_revision",
    "nickname_revision",
    "incarnation_generation",
    "placement",
    "owner_fencing_token",
    "tombstoned",
    "cancellation_epoch",
}
_REQUEST = {
    "request_id",
    "root_agent_id",
    "parent_agent_id",
    "agent_path",
    "nickname",
    "definition_id",
    "capacity_reservation_id",
    "budget_reservation_ids",
}
_ADMISSION = {
    "admission_id",
    "create_request_id",
    "run_id",
    "definition_id",
    "logical_agent_id",
    "root_agent_id",
    "lineage_revision",
    "cancellation_epoch",
    "revision",
    "ownership",
    "lifecycle",
}
_OWNERSHIP = {"request", "subject", "fencing_token", "expires_at"}
_OWNERSHIP_REQUEST = {
    "deployment_id",
    "operation_id",
    "holder_id",
    "backend",
    "expected_revision",
    "effect_id",
    "effect_capability",
}


def encode_lineage(
    records: tuple[LineageRecord, ...],
    admissions: tuple[WorkflowCreateAdmission, ...],
    revision: int,
) -> bytes:
    payload = {
        "schema": LINEAGE_SCHEMA,
        "revision": revision,
        "records": [_encode_record(record) for record in records],
        "workflow_create_admissions": [_encode_admission(item) for item in admissions],
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def decode_lineage(data: bytes) -> tuple[int, tuple[LineageRecord, ...], tuple[WorkflowCreateAdmission, ...]]:
    try:
        raw = json.loads(data.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Agent lineage is not canonical JSON") from exc
    top = _mapping(raw, _TOP, "lineage envelope")
    if top["schema"] != LINEAGE_SCHEMA:
        raise ValueError("Agent lineage schema is unsupported")
    revision = _nonnegative_int(top["revision"], "lineage revision")
    rows = top["records"]
    if type(rows) is not list:
        raise ValueError("Agent lineage records must be a list")
    records = tuple(_decode_record(row) for row in rows)
    if records and max(record.revision for record in records) > revision:
        raise ValueError("Agent lineage record revision exceeds projection revision")
    request_ids = [record.request.request_id for record in records]
    if len(request_ids) != len(set(request_ids)):
        raise ValueError("Agent lineage request identity is duplicated")
    admission_rows = top["workflow_create_admissions"]
    if type(admission_rows) is not list:
        raise ValueError("Workflow create admissions must be a list")
    admissions = tuple(_decode_admission(row) for row in admission_rows)
    admission_ids = [item.admission_id for item in admissions]
    request_keys = [item.create_request_id for item in admissions]
    run_ids = [item.reference.run_id for item in admissions]
    if (
        len(admission_ids) != len(set(admission_ids))
        or len(request_keys) != len(set(request_keys))
        or len(run_ids) != len(set(run_ids))
    ):
        raise ValueError("Workflow create admission stable mapping is duplicated")
    if any(item.revision > revision for item in admissions):
        raise ValueError("Workflow admission revision exceeds lineage revision")
    return revision, records, admissions


def _encode_admission(item: WorkflowCreateAdmission) -> dict[str, object]:
    return {
        "admission_id": str(item.admission_id),
        "create_request_id": item.create_request_id,
        "run_id": str(item.reference.run_id),
        "definition_id": str(item.reference.definition_id),
        "logical_agent_id": str(item.logical_agent_id),
        "root_agent_id": str(item.root_agent_id),
        "lineage_revision": int(item.lineage_revision),
        "cancellation_epoch": int(item.cancellation_epoch),
        "revision": item.revision,
        "ownership": _encode_ownership(item.ownership),
        "lifecycle": item.lifecycle.value,
    }


def _decode_admission(raw: object) -> WorkflowCreateAdmission:
    row = _mapping(raw, _ADMISSION, "Workflow create admission")
    return WorkflowCreateAdmission(
        WorkflowCreateAdmissionId(_string(row["admission_id"], "admission_id")),
        _string(row["create_request_id"], "create_request_id"),
        WorkflowRunReference(
            WorkflowRunId(_string(row["run_id"], "run_id")),
            WorkflowDefinitionId(_string(row["definition_id"], "definition_id")),
        ),
        AgentId(_string(row["logical_agent_id"], "logical_agent_id")),
        AgentId(_string(row["root_agent_id"], "root_agent_id")),
        LineageRevision(_positive_int(row["lineage_revision"], "lineage_revision")),
        CancellationEpoch(_nonnegative_int(row["cancellation_epoch"], "cancellation_epoch")),
        _positive_int(row["revision"], "admission revision"),
        _decode_ownership(row["ownership"]),
        WorkflowCreateAdmissionLifecycle(_string(row["lifecycle"], "admission lifecycle")),
    )


def _encode_ownership(item: OperationOwnership) -> dict[str, object]:
    request = item.request
    return {
        "request": {
            "deployment_id": request.deployment_id,
            "operation_id": request.operation_id,
            "holder_id": request.holder_id,
            "backend": request.backend.value,
            "expected_revision": request.expected_revision,
            "effect_id": request.effect_id,
            "effect_capability": request.effect_capability.value,
        },
        "subject": item.subject,
        "fencing_token": item.fencing_token,
        "expires_at": item.expires_at,
    }


def _decode_ownership(raw: object) -> OperationOwnership:
    row = _mapping(raw, _OWNERSHIP, "operation ownership")
    request = _mapping(row["request"], _OWNERSHIP_REQUEST, "operation ownership request")
    expires_at = row["expires_at"]
    if type(expires_at) not in {int, float}:
        raise ValueError("operation ownership expiry must be numeric")
    expires_number = cast(int | float, expires_at)
    return OperationOwnership(
        OperationOwnershipRequest(
            _string(request["deployment_id"], "deployment_id"),
            _string(request["operation_id"], "operation_id"),
            _string(request["holder_id"], "holder_id"),
            OperationBackend(_string(request["backend"], "operation backend")),
            _nonnegative_int(request["expected_revision"], "operation expected revision"),
            _string(request["effect_id"], "effect_id"),
            EffectCapability(_string(request["effect_capability"], "effect capability")),
        ),
        _string(row["subject"], "operation subject"),
        _positive_int(row["fencing_token"], "operation fencing token"),
        float(expires_number),
    )


def _encode_record(record: LineageRecord) -> dict[str, object]:
    request = record.request
    return {
        "request": {
            "request_id": request.request_id,
            "root_agent_id": request.root_agent_id,
            "parent_agent_id": request.parent_agent_id,
            "agent_path": request.agent_path,
            "nickname": request.nickname,
            "definition_id": request.definition_id,
            "capacity_reservation_id": request.capacity_reservation_id,
            "budget_reservation_ids": list(request.budget_reservation_ids),
        },
        "logical_agent_id": record.logical_agent_id,
        "lifecycle": record.lifecycle.value,
        "revision": record.revision,
        "path_revision": record.path_revision,
        "nickname_revision": record.nickname_revision,
        "incarnation_generation": record.incarnation_generation,
        "placement": record.placement,
        "owner_fencing_token": record.owner_fencing_token,
        "cancellation_epoch": record.cancellation_epoch,
        "tombstoned": record.tombstoned,
    }


def _decode_record(raw: object) -> LineageRecord:
    row = _mapping(raw, _RECORD, "lineage record")
    request_row = _mapping(row["request"], _REQUEST, "spawn request")
    budget = request_row["budget_reservation_ids"]
    if type(budget) is not list or any(type(item) is not str or not item for item in budget):
        raise ValueError("spawn budget reservation identities are invalid")
    lifecycle_raw = _string(row["lifecycle"], "spawn lifecycle")
    try:
        lifecycle = SpawnLifecycle(lifecycle_raw)
    except ValueError as exc:
        raise ValueError("spawn lifecycle is unsupported") from exc
    return LineageRecord(
        SpawnRequest(
            _string(request_row["request_id"], "request_id"),
            _string(request_row["root_agent_id"], "root_agent_id"),
            _optional_string(request_row["parent_agent_id"], "parent_agent_id"),
            _string(request_row["agent_path"], "agent_path"),
            _optional_string(request_row["nickname"], "nickname"),
            _string(request_row["definition_id"], "definition_id"),
            _string(request_row["capacity_reservation_id"], "capacity_reservation_id"),
            tuple(budget),
        ),
        _optional_string(row["logical_agent_id"], "logical_agent_id"),
        lifecycle,
        _positive_int(row["revision"], "record revision"),
        _positive_int(row["path_revision"], "path revision"),
        _optional_positive_int(row["nickname_revision"], "nickname revision"),
        _nonnegative_int(row["incarnation_generation"], "incarnation generation"),
        _optional_string(row["placement"], "placement"),
        _positive_int(row["owner_fencing_token"], "owner fence"),
        _nonnegative_int(row["cancellation_epoch"], "cancellation epoch"),
        _boolean(row["tombstoned"], "tombstoned"),
    )


def _mapping(raw: object, fields: set[str], label: str) -> dict[str, object]:
    if type(raw) is not dict or set(raw) != fields:
        raise ValueError(f"{label} fields are not canonical")
    return cast(dict[str, object], raw)


def _string(raw: object, label: str) -> str:
    if type(raw) is not str or not raw:
        raise ValueError(f"{label} is invalid")
    return raw


def _optional_string(raw: object, label: str) -> str | None:
    return None if raw is None else _string(raw, label)


def _nonnegative_int(raw: object, label: str) -> int:
    if type(raw) is not int or raw < 0:
        raise ValueError(f"{label} is invalid")
    return raw


def _positive_int(raw: object, label: str) -> int:
    value = _nonnegative_int(raw, label)
    if value < 1:
        raise ValueError(f"{label} is invalid")
    return value


def _optional_positive_int(raw: object, label: str) -> int | None:
    return None if raw is None else _positive_int(raw, label)


def _boolean(raw: object, label: str) -> bool:
    if type(raw) is not bool:
        raise ValueError(f"{label} is invalid")
    return raw


__all__ = ["LINEAGE_SCHEMA", "decode_lineage", "encode_lineage"]
