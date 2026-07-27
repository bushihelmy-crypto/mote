"""Session-log journal for durable Runtime projection work."""
from __future__ import annotations

from mote.contracts.runtimes import RuntimeCommitFact, RuntimeProjectionAck
from mote.runtime.session.events import RuntimeCommitEvent, RuntimeProjectionAcknowledgedEvent
from mote.runtime.session.log import SessionLog


class SessionRuntimeProjectionJournal:
    """Append Runtime commit facts and acknowledgements to one rollout."""

    def __init__(self, log: SessionLog) -> None:
        self._log = log

    @property
    def log(self) -> SessionLog:
        return self._log

    async def record_commit(self, fact: RuntimeCommitFact) -> None:
        await self._log.append(RuntimeCommitEvent(fact))

    async def acknowledge(self, ack: RuntimeProjectionAck) -> None:
        await self._log.append(RuntimeProjectionAcknowledgedEvent(ack))


__all__ = ["SessionRuntimeProjectionJournal"]
