"""Durable, append-only audit authority for Shared daemon operations."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from mote.contracts.events.envelope import EventId, EventType, JsonValue, StreamId
from mote.contracts.ports.events.journal import UncommittedFact
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
        payload: dict[str, JsonValue] = {
            "operation": operation,
            "outcome": outcome,
            "details": {key: str(value) for key, value in details.items()},
        }
        result = await self._journal.append(
            _STREAM,
            (
                UncommittedFact(
                    event_id=EventId(str(uuid4())),
                    event_type=EventType("mote.inference.operations-audit"),
                    schema_version=1,
                    occurred_at=datetime.now(timezone.utc),
                    payload=payload,
                ),
            ),
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
