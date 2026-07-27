"""Durable journal contract for managed Runtime ownership handoff."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from mote.contracts.handoff import RuntimeHandoffIntent, RuntimeHandoffRecovery, RuntimeHandoffResolution
from mote.contracts.runtimes import RuntimeCheckpoint


@runtime_checkable
class RuntimeHandoffJournal(Protocol):
    async def prepare(self, intent: RuntimeHandoffIntent) -> None:
        ...

    async def activate(self, handoff_id: str) -> None:
        ...

    async def resolve(self, resolution: RuntimeHandoffResolution) -> None:
        ...

    async def recovery(
        self,
        *,
        kind: str,
        alias: str,
        checkpoint: RuntimeCheckpoint | None,
    ) -> RuntimeHandoffRecovery:
        ...


__all__ = ["RuntimeHandoffJournal"]
