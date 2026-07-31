"""Bind the active loss-tolerant telemetry runtime to the async context.

Mirrors ``runtime/logging/context.py``'s ``bind_trace``: a :class:`contextvars.ContextVar`
holding the telemetry runtime the current turn is running under, so deep call sites (the LLM
client streaming tokens, a tool capturing a snapshot) can fan an **observation**
onto the same runtime without threading Telemetry through every signature.

This contextvar transports observation facts only. Control decisions use typed
domain Policies and never travel through Telemetry, so losing this context in a
spawned task can only drop a best-effort observation.

The router layer reads only :func:`current_telemetry` / :func:`observe_event` — it never
imports the concrete runtime owner (Role). When none is bound (standalone client
use, tests), these are no-ops.

An unbound emit is a silent no-op. Whether another Engine has telemetry is irrelevant:
diagnostics must not use process-global history to classify this context's state.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

from mote.kernel.telemetry.context import bind_trace_id_provider
from mote.kernel.telemetry.events import bind_observers
from mote.runtime.events.telemetry import TelemetryRuntime
from mote.runtime.telemetry.logging import current_trace_id

_ACTIVE_TELEMETRY: ContextVar[Optional[TelemetryRuntime]] = ContextVar(
    "mote_telemetry_runtime",
    default=None,
)


def current_telemetry() -> Optional[TelemetryRuntime]:
    """Return the telemetry runtime bound in this context, if any."""
    return _ACTIVE_TELEMETRY.get()


@contextmanager
def bind_telemetry(
    telemetry: Optional[TelemetryRuntime],
) -> Iterator[Optional[TelemetryRuntime]]:
    """Bind ``telemetry`` for the duration of the ``with`` block."""
    token = _ACTIVE_TELEMETRY.set(telemetry)
    try:
        with bind_trace_id_provider(current_trace_id if telemetry is not None else None):
            with bind_observers(
                observe_event if telemetry is not None else None,
                observe_event_sync if telemetry is not None else None,
            ):
                yield telemetry
    finally:
        _ACTIVE_TELEMETRY.reset(token)


async def observe_event(event) -> None:
    """Publish ``event`` on the active telemetry runtime; no-op if unbound."""
    telemetry = _ACTIVE_TELEMETRY.get()
    if telemetry is None:
        return
    await telemetry.emit(event)


def observe_event_sync(event) -> None:
    """Sync fire-and-forget observation on active telemetry; no-op if unbound."""
    telemetry = _ACTIVE_TELEMETRY.get()
    if telemetry is None:
        return
    telemetry.emit_sync(event)


__all__ = [
    "bind_telemetry",
    "current_telemetry",
    "observe_event",
    "observe_event_sync",
]
