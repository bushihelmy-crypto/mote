"""Output and runtime-durability event projections."""

from __future__ import annotations

from typing import Optional

from mote.contracts.events.output import OutputCommittedEvent, OutputSnapshotEvent, OutputSnapshotInvalidatedEvent
from mote.contracts.events.session import RuntimeDurabilityChangedEvent
from mote.product.presentation.events.events import (
    OutputCommitted,
    OutputSnapshot,
    OutputSnapshotInvalidated,
    RuntimeDurabilityStatus,
    ViewEvent,
)
from mote.product.presentation.input_events import PresentationInputEvent


def project_output_event(event: PresentationInputEvent) -> Optional[list[ViewEvent]]:
    if isinstance(event, OutputSnapshotEvent):
        return [
            OutputSnapshot(
                run_id=event.run_id,
                revision=event.revision,
                schema_fingerprint=event.schema_fingerprint,
                value=event.value,
            )
        ]
    if isinstance(event, OutputSnapshotInvalidatedEvent):
        return [
            OutputSnapshotInvalidated(
                run_id=event.run_id,
                revision=event.revision,
                reason=event.reason,
            )
        ]
    if isinstance(event, OutputCommittedEvent):
        return [
            OutputCommitted(
                run_id=event.run_id,
                run_kind=event.run_kind,
                contract_id=event.contract_id,
                schema_fingerprint=event.schema_fingerprint,
                value=event.value,
            )
        ]
    if isinstance(event, RuntimeDurabilityChangedEvent):
        return [
            RuntimeDurabilityStatus(
                runtime_id=event.runtime_id,
                runtime_kind=event.runtime_kind,
                alias=event.alias,
                state=event.state,
                current_revision=event.current_revision,
                recoverable_revision=event.recoverable_revision,
                detail=event.detail,
            )
        ]
    return None


__all__ = ["project_output_event"]
