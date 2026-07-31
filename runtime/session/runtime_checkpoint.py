"""Session-log sink for managed Runtime checkpoints."""
from __future__ import annotations

from mote.contracts.runtime import RuntimeCheckpoint
from mote.runtime.session.events import RuntimeCheckpointEvent
from mote.runtime.session.log import SessionLog


class RuntimeCheckpointRecorder:
    """Append Runtime checkpoints to the session's durable rollout."""

    def __init__(self, log: SessionLog) -> None:
        self._log = log

    @property
    def log(self) -> SessionLog:
        return self._log

    async def persist(
        self,
        checkpoint: RuntimeCheckpoint,
        *,
        reason: str,
    ) -> None:
        await self._log.append(RuntimeCheckpointEvent(checkpoint=checkpoint, reason=reason))


__all__ = ["RuntimeCheckpointRecorder"]
