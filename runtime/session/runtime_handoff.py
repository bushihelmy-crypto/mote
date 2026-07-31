"""Rollout-backed ownership handoff journal and local crash recovery."""
from __future__ import annotations

from dataclasses import replace

from mote.contracts.runtime import RuntimeCheckpoint
from mote.contracts.runtime.handoff import RuntimeHandoffIntent, RuntimeHandoffRecovery, RuntimeHandoffResolution
from mote.runtime.session.events import (
    RuntimeHandoffActivatedEvent,
    RuntimeHandoffPreparedEvent,
    RuntimeHandoffResolvedEvent,
)
from mote.runtime.session.log import SessionLog
from mote.runtime.session.replay import replay


class SessionRuntimeHandoffJournal:
    """Persist transfer phases and reclaim unfinished local handoffs on restart."""

    def __init__(self, log: SessionLog) -> None:
        self._log = log

    async def prepare(self, intent: RuntimeHandoffIntent) -> None:
        await self._log.append(RuntimeHandoffPreparedEvent(intent))

    async def activate(self, handoff_id: str) -> None:
        await self._log.append(RuntimeHandoffActivatedEvent(handoff_id))

    async def resolve(self, resolution: RuntimeHandoffResolution) -> None:
        await self._log.append(RuntimeHandoffResolvedEvent(resolution=resolution))

    async def recovery(
        self,
        *,
        kind: str,
        alias: str,
        checkpoint: RuntimeCheckpoint | None,
    ) -> RuntimeHandoffRecovery:
        state = replay(self._log)
        current = checkpoint or state.runtime_checkpoints.get(f"{kind}:{alias}")
        pending = [
            item
            for item in state.pending_runtime_handoffs.values()
            if item.intent.kind == kind and item.intent.alias == alias
        ]
        if not pending:
            latest = state.runtime_handoff_resolutions.get(f"{kind}:{alias}")
            return RuntimeHandoffRecovery(
                runtime_id=(
                    current.runtime_id if current is not None else (latest.runtime_id if latest is not None else None)
                ),
                epoch=(current.epoch if current is not None else (latest.epoch if latest is not None else None)),
                revision=(
                    current.revision if current is not None else (latest.revision if latest is not None else None)
                ),
                checkpoint=current,
            )

        recovered_ids: list[str] = []
        runtime_id: str | None = current.runtime_id if current is not None else None
        epoch: int | None = current.epoch if current is not None else None
        revision: int | None = current.revision if current is not None else None
        for item in pending:
            intent = item.intent
            if current is None or current.runtime_id != intent.runtime_id:
                current = intent.base_checkpoint
            runtime_id = current.runtime_id if current is not None else intent.runtime_id
            epoch = current.epoch if current is not None else intent.epoch
            revision = current.revision if current is not None else intent.base_revision
            if (
                item.active
                and current is not None
                and current.runtime_id == intent.runtime_id
                and current.epoch == intent.epoch
                and current.revision == intent.base_revision
                and self._checkpoint_changed(intent.base_checkpoint, current)
            ):
                current = replace(current, revision=intent.target_revision)
                revision = current.revision
            recovered_ids.append(intent.handoff_id)

        return RuntimeHandoffRecovery(
            runtime_id=runtime_id,
            epoch=epoch,
            revision=revision,
            checkpoint=current,
            recovered_handoff_ids=tuple(recovered_ids),
        )

    @staticmethod
    def _checkpoint_changed(
        before: RuntimeCheckpoint | None,
        after: RuntimeCheckpoint,
    ) -> bool:
        if before is None:
            return True
        if before.digest and after.digest:
            return before.digest != after.digest
        return before.payload_ref != after.payload_ref


__all__ = ["SessionRuntimeHandoffJournal"]
