"""Ordered drain-before-close lifecycle for one model Runtime generation."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol


class AsyncCloseable(Protocol):
    async def aclose(self) -> None: ...


class AsyncDrainable(Protocol):
    async def drain(self, *, timeout_seconds: float) -> None: ...


@dataclass(frozen=True, slots=True)
class AsyncCloseAdapter:
    """Explicitly adapt a differently named async close operation."""

    close_operation: Callable[[], Awaitable[None]]

    async def aclose(self) -> None:
        await self.close_operation()


class GenerationLifecycle:
    def __init__(
        self,
        closeables: Iterable[AsyncCloseable],
        *,
        drainables: Iterable[AsyncDrainable] = (),
        drain_timeout_seconds: float = 30.0,
    ) -> None:
        if drain_timeout_seconds <= 0:
            raise ValueError("generation drain timeout must be positive")
        unique: dict[int, AsyncCloseable] = {}
        for resource in closeables:
            unique.setdefault(id(resource), resource)
        self._resources = tuple(unique.values())
        unique_drains: dict[int, AsyncDrainable] = {}
        for resource in drainables:
            unique_drains.setdefault(id(resource), resource)
        self._drainables = tuple(unique_drains.values())
        self._drain_timeout_seconds = drain_timeout_seconds
        self._closed = False

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        errors: list[BaseException] = []
        for resource in self._drainables:
            try:
                await resource.drain(timeout_seconds=self._drain_timeout_seconds)
            except BaseException as exc:
                errors.append(exc)
        for resource in self._resources:
            try:
                await resource.aclose()
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise BaseExceptionGroup("Runtime generation lifecycle failed", errors)


__all__ = ["AsyncCloseAdapter", "AsyncCloseable", "AsyncDrainable", "GenerationLifecycle"]
