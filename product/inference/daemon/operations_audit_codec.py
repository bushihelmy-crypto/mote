"""Typed durable codec for Shared daemon operation audit facts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from uuid import uuid4

from mote.contracts.events.envelope import EventId, EventType, JsonValue
from mote.contracts.events.governance import (
    ArtifactPolicy,
    CodecState,
    CompactionDisposition,
    EventCodecEntry,
    Sensitivity,
    StoragePolicy,
)
from mote.contracts.ports.events.journal import UncommittedFact

OPERATIONS_AUDIT_EVENT_TYPE = EventType("mote.inference.operations-audit")
OPERATIONS_AUDIT_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class OperationsAuditEvent:
    operation: str
    outcome: str
    details: Mapping[str, str]


def decode_operations_audit_event(payload: object) -> OperationsAuditEvent:
    if type(payload) is not dict or set(payload) != {"operation", "outcome", "details"}:
        raise ValueError("operations audit payload is not canonical")
    operation = payload["operation"]
    outcome = payload["outcome"]
    details = payload["details"]
    if type(operation) is not str or not operation or type(outcome) is not str or not outcome:
        raise ValueError("operations audit identity is invalid")
    if type(details) is not dict or any(
        type(key) is not str or type(value) is not str for key, value in details.items()
    ):
        raise ValueError("operations audit details must be text pairs")
    return OperationsAuditEvent(operation, outcome, MappingProxyType(dict(details)))


def _validate_operations_audit_event(event: OperationsAuditEvent) -> None:
    decode_operations_audit_event(
        {"operation": event.operation, "outcome": event.outcome, "details": dict(event.details)}
    )


def encode_operations_audit_event(event: OperationsAuditEvent) -> UncommittedFact:
    _validate_operations_audit_event(event)
    payload: dict[str, JsonValue] = {
        "operation": event.operation,
        "outcome": event.outcome,
        "details": dict(event.details),
    }
    return UncommittedFact(
        event_id=EventId(str(uuid4())),
        event_type=OPERATIONS_AUDIT_EVENT_TYPE,
        schema_version=OPERATIONS_AUDIT_SCHEMA_VERSION,
        occurred_at=datetime.now(timezone.utc),
        payload=payload,
    )


OPERATIONS_AUDIT_ACTIVE_CODEC = EventCodecEntry(
    logical_store="inference-operations-audit",
    event_family="operations-audit",
    event_type=OPERATIONS_AUDIT_EVENT_TYPE,
    event_schema_version=OPERATIONS_AUDIT_SCHEMA_VERSION,
    store_generation=1,
    state=CodecState.ACTIVE,
    owner_id="inference-daemon",
    encoder=encode_operations_audit_event,
    decoder=decode_operations_audit_event,
    validator=_validate_operations_audit_event,
    policy=StoragePolicy(
        sensitivity=Sensitivity.RESTRICTED,
        semantic_inline_size_limit=16 * 1024,
        retention_requirement="retain for the complete daemon authority lifetime; deletion occurs only when the entire authority is decommissioned",
        redaction_at_source=True,
        compaction_disposition=CompactionDisposition.RETAIN,
        legal_hold_behavior="authority-lifetime retention is already stronger than legal hold; decommissioning must honor any active hold",
        artifact_policy=ArtifactPolicy.FORBIDDEN,
        secondary_copy_policy="logs contain operation identity and outcome only",
    ),
)


__all__ = [
    "OPERATIONS_AUDIT_ACTIVE_CODEC",
    "OperationsAuditEvent",
    "decode_operations_audit_event",
    "encode_operations_audit_event",
]
