"""Cancellation-safe, phased ownership for Runtime resources."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import TypeAlias

SyncCloseCallback: TypeAlias = Callable[[], None]
AsyncCloseCallback: TypeAlias = Callable[[], Awaitable[None]]
CloseCallback: TypeAlias = SyncCloseCallback | AsyncCloseCallback


def _bind_close(close: CloseCallback) -> AsyncCloseCallback:
    """Classify a close callback once, before it enters lifecycle execution."""

    if inspect.iscoroutinefunction(close):
        return close

    async def invoke_sync() -> None:
        result = close()
        if result is not None:
            raise TypeError("synchronous lifecycle close callback returned a value")

    return invoke_sync


class LifecycleState(str, Enum):
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"


class LifecyclePhase(IntEnum):
    """Cross-runtime shutdown ordering contract."""

    STOP_PRODUCERS = 100
    CLOSE_RESOURCES = 200
    FLUSH_EXPORTERS = 300
    FLUSH_DURABILITY = 400
    RELEASE_CONTAINER = 1000


@dataclass(frozen=True, slots=True)
class ResourceCloseFailure:
    name: str
    error: BaseException


class LifecycleCloseError(RuntimeError):
    """One phase failed after every sibling resource was attempted."""

    def __init__(self, phase: int, failures: list[ResourceCloseFailure]) -> None:
        self.phase = phase
        self.failures = tuple(failures)
        details = "; ".join(f"{failure.name}: {type(failure.error).__name__}: {failure.error}" for failure in failures)
        super().__init__(f"lifecycle phase {phase} completed with {len(failures)} failure(s): {details}")


@dataclass(frozen=True, slots=True)
class LifecycleResource:
    """One explicitly named close action in a dependency-ordered phase."""

    name: str
    phase: int
    close: CloseCallback


@dataclass(frozen=True, slots=True)
class _BoundLifecycleResource:
    name: str
    phase: int
    close: AsyncCloseCallback


class LifecycleStack:
    """Own resources and close them by phase, LIFO within each phase.

    A failed phase blocks all later phases, retaining only failed resources for
    retry. This prevents downstream durability resources from disappearing
    while an upstream provider/exporter still needs them. Concurrent callers
    await one shielded task, so cancelling a waiter never cancels shutdown.
    """

    def __init__(self) -> None:
        self._resources: dict[str, _BoundLifecycleResource] = {}
        self._state = LifecycleState.OPEN
        self._close_task: asyncio.Task[None] | None = None

    @property
    def state(self) -> LifecycleState:
        return self._state

    @property
    def resource_names(self) -> tuple[str, ...]:
        return tuple(self._resources)

    def register(self, resource: LifecycleResource) -> None:
        if self._state is not LifecycleState.OPEN:
            raise RuntimeError(f"lifecycle is {self._state.value}; resources cannot be registered")
        normalized = resource.name.strip()
        if not normalized:
            raise ValueError("lifecycle resource name must not be empty")
        if normalized in self._resources:
            raise ValueError(f"lifecycle resource {normalized!r} is already registered")
        self._resources[normalized] = _BoundLifecycleResource(
            normalized,
            resource.phase,
            _bind_close(resource.close),
        )

    def register_close(self, name: str, close: CloseCallback, *, phase: int) -> None:
        self.register(LifecycleResource(name=name, phase=phase, close=close))

    async def aclose(self) -> None:
        if self._state is LifecycleState.CLOSED:
            return
        task = self._close_task
        if task is None or task.cancelled() or (task.done() and task.exception() is not None):
            task = asyncio.create_task(self._close(), name="mote-lifecycle-close")
            self._close_task = task
        await asyncio.shield(task)

    async def _close(self) -> None:
        self._state = LifecycleState.CLOSING
        while self._resources:
            phase = min(resource.phase for resource in self._resources.values())
            resources = [resource for resource in reversed(tuple(self._resources.values())) if resource.phase == phase]
            failures: list[ResourceCloseFailure] = []
            for resource in resources:
                try:
                    await resource.close()
                except Exception as exc:  # close every sibling before surfacing the phase
                    failures.append(ResourceCloseFailure(resource.name, exc))
                else:
                    self._resources.pop(resource.name, None)
            if failures:
                raise LifecycleCloseError(phase, failures)
        self._state = LifecycleState.CLOSED


__all__ = [
    "CloseCallback",
    "AsyncCloseCallback",
    "LifecycleCloseError",
    "LifecyclePhase",
    "LifecycleResource",
    "LifecycleStack",
    "LifecycleState",
    "ResourceCloseFailure",
    "SyncCloseCallback",
]
