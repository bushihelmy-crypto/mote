"""BackgroundPool protocol — the background-task-pool slice."""

from __future__ import annotations

from typing import Any, Protocol


class BackgroundPool(Protocol):
    """The background-task-pool slice the loop reads each idle turn.

    The loop pulls this fresh via ``get_bg_pool()`` because a tool may create
    the pool mid-react; it only ever inspects pending state and waits.
    """

    def has_pending(self) -> bool:
        """True while any submitted background task is still running."""
        ...

    @property
    def pending_count(self) -> int:
        """Number of tasks still running."""
        ...

    async def wait_any(self, timeout: float = ...) -> Any:
        """Block until any background task completes (or timeout)."""
        ...

    async def wait_for_completion(self, timeout: float | None = ...) -> bool:
        """Block until the next background task completes, or *timeout* elapses.

        Resolves on the next completion after the call (no ``has_pending``
        self-check). ``timeout`` defaults to a safety bound (10 min) so a bare
        call on an idle/empty pool returns instead of blocking forever; ``None``
        waits unbounded. Returns ``True`` on completion, ``False`` on timeout.
        """
        ...
