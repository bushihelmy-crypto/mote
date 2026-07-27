"""Stable lower-layer contract for Agent interaction with background work."""

from __future__ import annotations

from typing import Any, Callable, Protocol


class BackgroundTaskService(Protocol):
    """The lifecycle surface consumed by Kernel and Runtime.

    Task creation, graph scheduling, persistence, and concrete pool policy stay
    in Orchestration. Lower layers depend only on this deliberately small port.
    """

    def has_pending(self) -> bool:
        ...

    @property
    def pending_count(self) -> int:
        ...

    async def wait_any(self, timeout: float = ...) -> Any:
        ...

    async def wait_for_completion(self, timeout: float | None = ...) -> bool:
        ...

    def set_wake(self, wake: Callable[[], None] | None) -> None:
        ...

    async def aclose(self) -> None:
        ...


BackgroundTaskServiceFactory = Callable[[object], BackgroundTaskService]


__all__ = ["BackgroundTaskService", "BackgroundTaskServiceFactory"]
