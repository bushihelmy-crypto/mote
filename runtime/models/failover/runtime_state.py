"""Atomic model-runtime generations with drain-before-close lifecycle."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from threading import RLock

from mote.contracts.ports.model_endpoint import ModelEndpointResolver
from mote.runtime.models.failover.planner import FailoverPlanner


@dataclass(frozen=True)
class ModelRuntimeGeneration:
    planner: FailoverPlanner
    endpoint_resolver: ModelEndpointResolver

    @property
    def revision(self) -> str:
        return self.planner.snapshot.revision


class ModelRuntimeLease:
    def __init__(self, owner: "AtomicModelRuntime", generation: ModelRuntimeGeneration) -> None:
        self._owner = owner
        self.generation = generation
        self._released = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._owner._release(self.generation)


class AtomicModelRuntime:
    """Expose one immutable generation to each call and drain retired resources."""

    def __init__(
        self,
        planner: FailoverPlanner,
        endpoint_resolver: ModelEndpointResolver,
    ) -> None:
        self._lock = RLock()
        self._current = ModelRuntimeGeneration(planner, endpoint_resolver)
        self._leases: dict[int, int] = {}
        self._retired: dict[int, ModelRuntimeGeneration] = {}

    @property
    def current(self) -> ModelRuntimeGeneration:
        with self._lock:
            return self._current

    def acquire(self) -> ModelRuntimeLease:
        with self._lock:
            generation = self._current
            key = id(generation)
            self._leases[key] = self._leases.get(key, 0) + 1
        return ModelRuntimeLease(self, generation)

    async def activate(
        self,
        planner: FailoverPlanner,
        endpoint_resolver: ModelEndpointResolver,
    ) -> str:
        replacement = ModelRuntimeGeneration(planner, endpoint_resolver)
        close_now: ModelRuntimeGeneration | None = None
        with self._lock:
            previous = self._current
            self._current = replacement
            key = id(previous)
            if self._leases.get(key, 0):
                self._retired[key] = previous
            else:
                close_now = previous
        if close_now is not None:
            await _close_resolver(close_now.endpoint_resolver)
        return replacement.revision

    async def _release(self, generation: ModelRuntimeGeneration) -> None:
        close_now: ModelRuntimeGeneration | None = None
        with self._lock:
            key = id(generation)
            remaining = self._leases.get(key, 0) - 1
            if remaining > 0:
                self._leases[key] = remaining
            else:
                self._leases.pop(key, None)
                close_now = self._retired.pop(key, None)
        if close_now is not None:
            await _close_resolver(close_now.endpoint_resolver)


async def _close_resolver(resolver: ModelEndpointResolver) -> None:
    close = getattr(resolver, "aclose", None) or getattr(resolver, "close", None)
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        await result


__all__ = ["AtomicModelRuntime", "ModelRuntimeGeneration", "ModelRuntimeLease"]
