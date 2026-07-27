"""Role-lifecycle scheduling for bounded single-host artifact collection."""

from __future__ import annotations

import asyncio
from typing import Protocol

from mote.contracts.fileops.models import FileOperationsHealth
from mote.runtime.fileops.artifact_gc import ArtifactGarbageCollectionCycle
from mote.runtime.fileops.artifact_lifecycle import ArtifactCatalogGenerationConflictError
from mote.runtime.logging import log_class

_NORMAL_INTERVAL_SECONDS = 300.0
_PRESSURE_INTERVAL_SECONDS = 5.0
_QUOTA_PRESSURE_THRESHOLD = 0.75
_MAX_DRAIN_CYCLES = 32


class ArtifactGarbageCollectionTarget(Protocol):
    def collect_artifacts(
        self,
        *,
        limit: int,
        expedited: bool,
    ) -> ArtifactGarbageCollectionCycle:
        ...

    def health(self) -> FileOperationsHealth:
        ...


@log_class(level="DEBUG", exclude={"start", "aclose"})
class ArtifactGarbageCollectionService:
    """Runs bounded GC cycles as an explicitly owned Role maintenance task."""

    def __init__(
        self,
        target: ArtifactGarbageCollectionTarget,
        *,
        batch_size: int,
    ) -> None:
        if type(batch_size) is not int or batch_size <= 0:
            raise ValueError("artifact garbage collection batch size must be positive")
        self._target = target
        self._batch_size = batch_size
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(
            self._run(),
            name="mote-artifact-garbage-collection",
        )

    async def run_once(
        self,
        *,
        expedited: bool = False,
    ) -> ArtifactGarbageCollectionCycle:
        return await asyncio.to_thread(
            self._target.collect_artifacts,
            limit=self._batch_size,
            expedited=expedited,
        )

    async def _health(self) -> FileOperationsHealth:
        return await asyncio.to_thread(self._target.health)

    async def _run(self) -> None:
        delay = 0.0
        while not self._stop.is_set():
            if delay and await self._wait_for_stop(delay):
                return
            try:
                health = await self._health()
                await self._drain(expedited=self._under_pressure(health))
                delay = self._next_interval(await self._health())
            except asyncio.CancelledError:
                raise
            except ArtifactCatalogGenerationConflictError:
                delay = _PRESSURE_INTERVAL_SECONDS
            except Exception:
                delay = _NORMAL_INTERVAL_SECONDS

    async def _wait_for_stop(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return True

    async def _drain(self, *, expedited: bool) -> None:
        for _ in range(_MAX_DRAIN_CYCLES):
            cycle = await self.run_once(expedited=expedited)
            if self._stop.is_set():
                return
            health = await self._health()
            expedited = expedited or self._under_pressure(health)
            saturated = (
                len(cycle.quarantine.quarantined_objects) == self._batch_size
                or len(cycle.deletion.deletion_candidates) == self._batch_size
                or len(cycle.reclamation.candidates) == self._batch_size
            )
            if health.artifact_deleting_objects == 0 and not saturated:
                return
            await asyncio.sleep(0)

    @staticmethod
    def _under_pressure(health: FileOperationsHealth) -> bool:
        return health.artifact_deleting_objects > 0 or health.artifact_quota_pressure >= _QUOTA_PRESSURE_THRESHOLD

    @classmethod
    def _next_interval(cls, health: FileOperationsHealth) -> float:
        if cls._under_pressure(health):
            return _PRESSURE_INTERVAL_SECONDS
        return _NORMAL_INTERVAL_SECONDS

    async def aclose(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        self._stop.set()
        await asyncio.gather(task, return_exceptions=True)


__all__ = [
    "ArtifactGarbageCollectionService",
    "ArtifactGarbageCollectionTarget",
]
