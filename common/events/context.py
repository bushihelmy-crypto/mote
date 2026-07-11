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

Dropping an observation is *always* survivable (see the control/observation split
above), so an unbound emit never raises. But a *silent* drop is a debugging trap:
forgetting to wrap a runtime in :func:`set_bus`, or emitting from a task spawned
without copying the context, makes every observation vanish with no signal. To
separate a legitimate standalone/test run (no bus is *meant* to exist) from a
production omission (a bus exists in this process but this call site fell outside
its scope), a process-global latch — :data:`_bus_ever_bound` — flips true the
first time a real bus is bound. An unbound emit *after* that point is a genuine
omission and earns a rate-limited warning (once per entrypoint); before it, the
drop is expected and stays quiet. Either way the emit remains a no-op.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional, Set

from mote.common.events.bus import EventBus
from mote.common.logs import logger

_ACTIVE_BUS: ContextVar[Optional[EventBus]] = ContextVar("mote_event_bus", default=None)

#: Flips true the first time a real (non-``None``) bus is bound anywhere in this
#: process — the proxy for "this is a wired agent runtime, not a bare library /
#: test use". Only ever set true; a standalone process that never calls
#: :func:`set_bus` leaves it false, so its unbound emits stay silent.
_bus_ever_bound: bool = False

#: Entrypoints already warned about (dedupe so a persistent misconfiguration
#: logs once per site, not once per event).
_warned_unbound: Set[str] = set()


def current_bus() -> Optional[EventBus]:
    """Return the bus bound in the current context, or ``None`` if unbound."""
    return _ACTIVE_BUS.get()


@contextmanager
def set_bus(bus: Optional[EventBus]) -> Iterator[Optional[EventBus]]:
    """Bind ``bus`` for the duration of the ``with`` block."""
    global _bus_ever_bound
    if bus is not None:
        _bus_ever_bound = True
    token = _ACTIVE_BUS.set(bus)
    try:
        yield bus
    finally:
        _ACTIVE_BUS.reset(token)


def _warn_if_unbound(entrypoint: str) -> None:
    """Warn (once per entrypoint) when an emit is dropped despite a live runtime.

    Silent when no bus was ever bound in this process (a legitimate standalone /
    test run). Once a real bus *has* been bound, an unbound emit means a call site
    fell outside the runtime's :func:`set_bus` scope (a forgotten wrap, or a task
    spawned without copying the context) — a dropped observation worth flagging.
    """
    if not _bus_ever_bound or entrypoint in _warned_unbound:
        return
    _warned_unbound.add(entrypoint)
    logger.warning(
        f"{entrypoint}: no event bus bound in this context — observation dropped. "
        "A bus exists in this process, so this call site is likely running outside "
        "the runtime's `set_bus` scope (e.g. a task spawned without copying context)."
    )


async def observe_event(event) -> None:
    """Fan ``event`` to observers on the active bus (phase 2 only); no-op if unbound.

    The deep-site transport. It runs the observation phase exclusively — it
    structurally cannot fold a control outcome, so it returns nothing. Control
    emitters call ``bus.emit`` on their explicit reference instead.
    """
    bus = _ACTIVE_BUS.get()
    if bus is None:
        _warn_if_unbound("observe_event")
        return
    await bus.observe(event)


def observe_event_sync(event) -> None:
    """Sync fire-and-forget observation on the active bus; no-op when unbound."""
    bus = _ACTIVE_BUS.get()
    if bus is None:
        _warn_if_unbound("observe_event_sync")
        return
    bus.emit_sync(event)


__all__ = ["current_bus", "set_bus", "observe_event", "observe_event_sync"]
