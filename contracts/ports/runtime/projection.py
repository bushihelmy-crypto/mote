"""Durable journal contract for Runtime commit projections."""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from mote.contracts.artifact import ArtifactPublicationIntent
from mote.contracts.runtime import RuntimeCommitFact, RuntimeProjectionAck, RuntimeProjectionRequest


@runtime_checkable
class RuntimeProjectionJournal(Protocol):
    """Persist Runtime commit facts and projection acknowledgements in order."""

    async def record_commit(self, fact: RuntimeCommitFact) -> None:
        ...

    async def acknowledge(self, ack: RuntimeProjectionAck) -> None:
        ...


@runtime_checkable
class RuntimeProjector(Protocol):
    """Materialize one versioned projection request into the trusted CAS."""

    projector: str
    schema_version: int

    async def project(
        self,
        request: RuntimeProjectionRequest,
    ) -> ArtifactPublicationIntent:
        ...


__all__ = ["RuntimeProjectionJournal", "RuntimeProjector"]
