"""Active-bus context — bind the current :class:`EventBus` to the async context.

Mirrors ``common/logs/context.py``'s ``bind_trace``: a :class:`contextvars.ContextVar`
holding the bus the current turn is running under, so deep call sites (the LLM
client streaming tokens, a tool capturing a snapshot) can ``emit`` onto the same
spine without the bus being threaded through every signature.

The router layer reads only :func:`current_bus` / :func:`emit_event` — it never
imports the concrete bus owner (Role). When no bus is bound (standalone client
use, tests), :func:`emit_event` is a no-op.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

from metagpt.common.events.bus import EventBus
from metagpt.common.events.outcome import EMPTY, HookOutcome

_ACTIVE_BUS: ContextVar[Optional[EventBus]] = ContextVar("metagpt_event_bus", default=None)


def current_bus() -> Optional[EventBus]:
    """Return the bus bound in the current context, or ``None`` if unbound."""
    return _ACTIVE_BUS.get()


@contextmanager
def set_bus(bus: Optional[EventBus]) -> Iterator[Optional[EventBus]]:
    """Bind ``bus`` for the duration of the ``with`` block."""
    token = _ACTIVE_BUS.set(bus)
    try:
        yield bus
    finally:
        _ACTIVE_BUS.reset(token)


async def emit_event(event) -> HookOutcome:
    """Emit ``event`` on the active bus, or no-op (returns ``EMPTY``) if unbound."""
    bus = _ACTIVE_BUS.get()
    if bus is None:
        return EMPTY
    return await bus.emit(event)


def emit_event_sync(event) -> None:
    """Sync fire-and-forget emit on the active bus; no-op when unbound."""
    bus = _ACTIVE_BUS.get()
    if bus is None:
        return
    bus.emit_sync(event)


__all__ = ["current_bus", "set_bus", "emit_event", "emit_event_sync"]
