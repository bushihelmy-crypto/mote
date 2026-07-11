"""HookRunner protocol — the narrow face consumers fire lifecycle events on.

The structural slice the ``ToolExecutor`` / ``ContextManager`` / ``Role`` depend
on to fire hooks, without naming the concrete ``HookManager``. A direct import of
``HookManager`` would also be legal (it lives in ``common``, the bottom layer),
but consumers take this Protocol — mirroring the other ``common.interface``
faces — so the subsystem stays swappable and the injection site is statically
checked.

Leaf module: imports only ``typing`` plus (under TYPE_CHECKING) ``HookOutcome``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Protocol, runtime_checkable

if TYPE_CHECKING:
    from mote.common.hook.types import HookOutcome


@runtime_checkable
class HookRunner(Protocol):
    """The single method consumers use to fire a lifecycle event."""

    async def fire(self, event: str, payload: dict, *, permission_mode: Optional[str] = None) -> "HookOutcome":
        """Run all handlers registered for ``event`` and return the folded outcome."""
        ...
