"""Durable sink contract for managed Runtime checkpoints."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from mote.contracts.runtime import RuntimeCheckpoint


@runtime_checkable
class RuntimeCheckpointSink(Protocol):
    """Persist the latest recoverable state of a managed Runtime."""

    async def persist(
        self,
        checkpoint: RuntimeCheckpoint,
        *,
        reason: str,
    ) -> None:
        ...


@runtime_checkable
class RuntimeCheckpointPayloadStore(Protocol):
    """Seal payloads for durable records and reopen them for Runtime drivers."""

    async def seal(self, checkpoint: RuntimeCheckpoint) -> RuntimeCheckpoint:
        ...

    async def open(self, checkpoint: RuntimeCheckpoint) -> RuntimeCheckpoint:
        ...


__all__ = ["RuntimeCheckpointPayloadStore", "RuntimeCheckpointSink"]
