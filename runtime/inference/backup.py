"""Fail-closed coordination and classification for recovery-set cuts."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from uuid import uuid4

from mote.contracts.inference.backup import BackupBarrierCut, BackupConsistency


@dataclass(frozen=True, slots=True)
class BackupParticipant:
    participant_id: str
    checkpoint: Callable[[int, int], Awaitable[bool]]

    def __post_init__(self) -> None:
        if not self.participant_id:
            raise ValueError("backup participant id is required")


class BackupEpochAuthority:
    """Single writer for permit-fencing backup and admission epochs."""

    def __init__(self) -> None:
        self._backup_epoch = 0
        self._admission_epoch = 0
        self._lock = asyncio.Lock()

    def current(self) -> tuple[int, int]:
        return self._backup_epoch, self._admission_epoch

    async def begin_cut(
        self,
        participants: Iterable[BackupParticipant],
        *,
        timeout_seconds: float,
        daemon_checkpoint_verified: bool,
        component_digests_verified: bool,
    ) -> BackupBarrierCut:
        if timeout_seconds <= 0:
            raise ValueError("backup barrier timeout must be positive")
        declared = tuple(participants)
        ids = tuple(item.participant_id for item in declared)
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("backup barrier participants must be non-empty and unique")
        async with self._lock:
            self._backup_epoch += 1
            self._admission_epoch += 1
            acknowledged: list[str] = []

            async def checkpoint(participant: BackupParticipant) -> bool:
                return await participant.checkpoint(self._backup_epoch, self._admission_epoch)

            tasks = tuple(asyncio.create_task(checkpoint(item)) for item in declared)
            done, pending = await asyncio.wait(tasks, timeout=timeout_seconds)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            outcomes = tuple(
                (task.result() if task in done and not task.cancelled() and task.exception() is None else False)
                for task in tasks
            )
            for item, outcome in zip(declared, outcomes, strict=True):
                if outcome is True:
                    acknowledged.append(item.participant_id)
            return BackupBarrierCut(
                backup_id=str(uuid4()),
                backup_epoch=self._backup_epoch,
                admission_epoch=self._admission_epoch,
                required_participants=ids,
                acknowledged_participants=tuple(acknowledged),
                daemon_checkpoint_verified=daemon_checkpoint_verified,
                component_digests_verified=component_digests_verified,
            )


def classify_backup_cut(cut: BackupBarrierCut) -> BackupConsistency:
    if not cut.daemon_checkpoint_verified or not cut.component_digests_verified:
        return BackupConsistency.CRASH_CONSISTENT
    if cut.missing_participants:
        return BackupConsistency.DAEMON_CONSISTENT
    return BackupConsistency.APPLICATION_CONSISTENT


__all__ = ["BackupEpochAuthority", "BackupParticipant", "classify_backup_cut"]
