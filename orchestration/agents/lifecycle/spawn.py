"""Atomic ownership transfer for child-Agent creation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import IntEnum
from typing import Awaitable, Callable


class SpawnPhase(IntEnum):
    NEW = 0
    ADMITTED = 1
    RESIDENCY_RESERVED = 2
    IDENTITY_RESERVED = 3
    CHILD_BUILT = 4
    PROVISIONED = 5
    REGISTERED_INERT = 6
    SUPERVISED = 7
    COMMITTED = 8
    ROLLED_BACK = 9


Reversal = Callable[[], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class SpawnCleanupFailure:
    phase: SpawnPhase
    error: BaseException


class SpawnRollbackError(RuntimeError):
    def __init__(self, failures: tuple[SpawnCleanupFailure, ...]) -> None:
        self.failures = failures
        super().__init__(f"spawn rollback had {len(failures)} cleanup failure(s)")


class SpawnTransaction:
    """Monotonic spawn transaction with reverse-order, idempotent rollback."""

    def __init__(self) -> None:
        self.phase = SpawnPhase.NEW
        self._reversals: list[tuple[SpawnPhase, Reversal]] = []
        self._committed = False
        self._rolled_back = False

    def advance(self, phase: SpawnPhase, reversal: Reversal | None = None) -> None:
        if self._committed or self._rolled_back:
            raise RuntimeError("spawn transaction is terminal")
        if phase <= self.phase or phase >= SpawnPhase.ROLLED_BACK:
            raise RuntimeError(f"invalid spawn phase transition {self.phase.name} -> {phase.name}")
        self.phase = phase
        if reversal is not None:
            self._reversals.append((phase, reversal))

    def own(self, reversal: Reversal) -> None:
        """Register another resource acquired within the current phase."""
        if self._committed or self._rolled_back:
            raise RuntimeError("spawn transaction is terminal")
        self._reversals.append((self.phase, reversal))

    def commit(self) -> None:
        if self.phase is not SpawnPhase.SUPERVISED:
            raise RuntimeError("spawn can commit only after supervision is installed")
        self.phase = SpawnPhase.COMMITTED
        self._committed = True
        self._reversals.clear()

    async def rollback(self) -> None:
        if self._committed or self._rolled_back:
            return
        failures: list[SpawnCleanupFailure] = []
        while self._reversals:
            phase, reversal = self._reversals.pop()
            try:
                result = reversal()
                if result is not None:
                    await result
            except BaseException as exc:  # noqa: BLE001
                failures.append(SpawnCleanupFailure(phase, exc))
        self._rolled_back = True
        self.phase = SpawnPhase.ROLLED_BACK
        if failures:
            raise SpawnRollbackError(tuple(failures))

    async def rollback_shielded(self) -> None:
        task = asyncio.create_task(self.rollback())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise


__all__ = [
    "SpawnCleanupFailure",
    "SpawnPhase",
    "SpawnRollbackError",
    "SpawnTransaction",
]
