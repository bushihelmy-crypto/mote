"""Active-bus context — bind the current :class:`EventBus` to the async context.

Mirrors ``common/logs/context.py``'s ``bind_trace``: a :class:`contextvars.ContextVar`
holding the bus the current turn is running under, so deep call sites (the LLM
client streaming tokens, a tool capturing a snapshot) can fan an **observation**
onto the same spine without the bus being threaded through every signature.

This contextvar transport carries *observation only*. The control plane (a hook
that may veto/mutate/stop) is always reached through an **explicit** bus
reference held by the emitter (executor / context manager / role), never through
the ambient contextvar — so a lost contextvar (e.g. a deep site spawned into a
new task without copying context) can only ever drop an observation, never a
veto. That is why the public deep-site entrypoints below are :func:`observe_event`
/ :func:`observe_event_sync`: structurally they cannot carry control, so the
inline-fold invariant cannot be broken from a deep call site by accident.

The router layer reads only :func:`current_bus` / :func:`observe_event` — it never
imports the concrete bus owner (Role). When no bus is bound (standalone client
use, tests), these are no-ops.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

from metagpt.common.events.bus import EventBus

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


async def observe_event(event) -> None:
    """Fan ``event`` to observers on the active bus (phase 2 only); no-op if unbound.

    The deep-site transport. It runs the observation phase exclusively — it
    structurally cannot fold a control outcome, so it returns nothing. Control
    emitters call ``bus.emit`` on their explicit reference instead.
    """
    bus = _ACTIVE_BUS.get()
    if bus is None:
        return
    await bus.observe(event)


def observe_event_sync(event) -> None:
    """Sync fire-and-forget observation on the active bus; no-op when unbound."""
    bus = _ACTIVE_BUS.get()
    if bus is None:
        return
    bus.emit_sync(event)


__all__ = ["current_bus", "set_bus", "observe_event", "observe_event_sync"]
