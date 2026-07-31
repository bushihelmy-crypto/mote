"""Kernel observation capability, bound by a hosting Runtime."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import Any
from uuid import uuid4

from mote.contracts.events.telemetry import SpanEndEvent, SpanStartEvent
from mote.kernel.telemetry.context import current_trace_id

AsyncTelemetryObserver = Callable[[Any], Awaitable[None]]
SyncTelemetryObserver = Callable[[Any], None]

_async_observer: ContextVar[AsyncTelemetryObserver | None] = ContextVar("mote_kernel_async_observer", default=None)
_sync_observer: ContextVar[SyncTelemetryObserver | None] = ContextVar("mote_kernel_sync_observer", default=None)
_current_span: ContextVar[str | None] = ContextVar("mote_kernel_span", default=None)


@contextmanager
def bind_observers(
    async_observer: AsyncTelemetryObserver | None,
    sync_observer: SyncTelemetryObserver | None,
) -> Iterator[None]:
    """Bind Runtime-provided observation capabilities for this execution scope."""
    async_token = _async_observer.set(async_observer)
    sync_token = _sync_observer.set(sync_observer)
    try:
        yield
    finally:
        _sync_observer.reset(sync_token)
        _async_observer.reset(async_token)


async def emit_event(event: Any) -> None:
    observer = _async_observer.get()
    if observer is not None:
        await observer(event)


def emit_event_sync(event: Any) -> None:
    observer = _sync_observer.get()
    if observer is not None:
        observer(event)


def current_span_id() -> str | None:
    return _current_span.get()


@asynccontextmanager
async def span(label: str, *, attributes: dict | None = None) -> AsyncIterator[str]:
    """Emit backend-neutral span records through the injected observer."""
    span_id = uuid4().hex
    trace_id = current_trace_id() or ""
    attrs = attributes or {}
    await emit_event(
        SpanStartEvent(
            span_id=span_id,
            parent_span_id=current_span_id(),
            trace_id=trace_id,
            label=label,
            attributes=attrs,
        )
    )
    token = _current_span.set(span_id)
    status, error = "ok", ""
    try:
        yield span_id
    except BaseException as exc:
        status, error = "error", f"{type(exc).__name__}: {exc}"
        raise
    finally:
        _current_span.reset(token)
        await emit_event(SpanEndEvent(span_id=span_id, trace_id=trace_id, status=status, error=error, attributes=attrs))


__all__ = ["bind_observers", "current_span_id", "emit_event", "emit_event_sync", "span"]
