"""Typed durable codec for Shared daemon operation audit facts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from uuid import uuid4

from mote.contracts.events.envelope import EventId, EventType, JsonValue
from mote.contracts.ports.events.journal import UncommittedFact

OPERATIONS_AUDIT_EVENT_TYPE = EventType("mote.inference.operations-audit")
OPERATIONS_AUDIT_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class OperationsAuditEvent:
    operation: str
    outcome: str
    details: Mapping[str, str]


def encode_operations_audit_event(event: OperationsAuditEvent) -> UncommittedFact:
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


__all__ = ["OperationsAuditEvent", "encode_operations_audit_event"]
