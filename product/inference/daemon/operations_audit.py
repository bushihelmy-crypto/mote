"""Durable, append-only audit authority for Shared daemon operations."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import MappingProxyType

from mote.contracts.events.envelope import StreamId
from mote.product.inference.daemon.operations_audit_codec import (
    OperationsAuditEvent,
    encode_operations_audit_event,
)
from mote.runtime.events import LocalEventJournal

_STREAM = StreamId("inference/shared-operations")


class SharedOperationsAudit:
    """Fail-closed mutation audit backed by the existing event journal."""

    def __init__(self, path: Path) -> None:
        self._journal = LocalEventJournal(path, _STREAM)
        report = self._journal.verify_committed(_STREAM)
        if not report.valid:
            raise RuntimeError("Shared operations audit journal failed verification")
        self._version = report.current_version

    @property
    def path(self) -> Path:
        return self._journal.path_for(_STREAM)

    async def record(self, operation: str, outcome: str, **details: object) -> None:
        event = OperationsAuditEvent(
            operation=operation,
            outcome=outcome,
            details=MappingProxyType({key: str(value) for key, value in details.items()}),
        )
        result = await self._journal.append(
            _STREAM,
            (encode_operations_audit_event(event),),
            expected_version=self._version,
        )
        self._version = result.current_version

    async def read(self, *, after: int = 0) -> AsyncIterator[dict[str, object]]:
        async for envelope in self._journal.read(_STREAM, after=after):
            yield {
                "sequence": envelope.sequence,
                "recorded_at": envelope.recorded_at.isoformat(),
                "operation": str(envelope.payload["operation"]),
                "outcome": str(envelope.payload["outcome"]),
            }


__all__ = ["SharedOperationsAudit"]
