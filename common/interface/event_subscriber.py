"""EventSubscriber protocol — the narrow face the :class:`EventBus` fans out to.

A subscriber declares a ``priority`` (ascending = earlier in dispatch) and an
async ``handle`` that consumes an event and optionally returns a
:class:`HookOutcome` (control events fold these; observation events ignore them).

An optional synchronous ``handle_sync`` lets a subscriber also receive
fire-and-forget observation events emitted from sync call sites (see
``EventBus.emit_sync``). It is not part of the required contract.

Leaf module: imports only ``typing`` plus (under TYPE_CHECKING) ``HookOutcome``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, Protocol, runtime_checkable

if TYPE_CHECKING:
    from metagpt.common.hook.types import HookOutcome


@runtime_checkable
class EventSubscriber(Protocol):
    """Something that consumes events off the bus in priority order."""

    priority: int

    async def handle(self, event) -> "Optional[HookOutcome]":
        """Handle one event; return a folded influence or ``None``."""
        ...


__all__ = ["EventSubscriber"]
