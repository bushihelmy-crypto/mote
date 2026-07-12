"""Framework-native trace context — the instrumentation primitive for spans.

OpenTelemetry-style tracing on our own spine: a contextvar holding the current
``span_id`` plus an ``async with span(...)`` primitive that emits
:class:`SpanStartEvent` / :class:`SpanEndEvent` carrying explicit
``span_id`` / ``parent_span_id`` / ``trace_id``. The trace *tree* is rebuilt
downstream from those IDs (by :class:`TracingSubscriber`), never from any
backend's ambient context — so tracing is backend-agnostic and a new exporter
is a new subscriber, not a spine change.

The ``trace_id`` is the existing logging trace-id (``common.logs.current_trace_id``,
already bound to the ``session_id`` via ``bind_trace``) — no new trace-id concept.

Leaf module: imports only ``common.events.context``, ``common.events.types`` and
``common.logs``. When no bus is bound (standalone client use / tests),
``observe_event`` is a no-op, so a ``span`` just mints a uuid and runs the body —
negligible cost. A span is pure observation (no veto), so it rides the
observation transport. Spans are part of the spine; exporters decide what to do.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import AsyncIterator, Optional
from uuid import uuid4

from metagpt.common.events.context import observe_event
from metagpt.common.events.types import SpanEndEvent, SpanStartEvent
from metagpt.common.logs import current_trace_id

_CURRENT_SPAN: ContextVar[Optional[str]] = ContextVar("metagpt_current_span", default=None)


def current_span_id() -> Optional[str]:
    """Return the span bound in the current context, or ``None`` if unbound."""
    return _CURRENT_SPAN.get()


@asynccontextmanager
async def span(label: str, *, attributes: Optional[dict] = None) -> AsyncIterator[str]:
    """Open a trace span around the wrapped block.

    Mints a ``span_id``, reads the ambient ``parent_span_id`` and ``trace_id``,
    emits a :class:`SpanStartEvent`, runs the body with the contextvar set to
    this span (so nested ``span`` calls link to it), and emits a
    :class:`SpanEndEvent` on exit — ``status="error"`` with the message captured
    when the body raises (the exception re-raises). Yields the ``span_id``.
    """
    span_id = uuid4().hex
    parent = current_span_id()
    trace_id = current_trace_id() or ""
    await observe_event(
        SpanStartEvent(
            span_id=span_id,
            parent_span_id=parent,
            trace_id=trace_id,
            label=label,
            attributes=attributes or {},
        )
    )
    token = _CURRENT_SPAN.set(span_id)
    status = "ok"
    error = ""
    try:
        yield span_id
    except BaseException as exc:  # noqa: BLE001 — capture, end the span, re-raise
        status = "error"
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        _CURRENT_SPAN.reset(token)
        await observe_event(
            SpanEndEvent(
                span_id=span_id,
                trace_id=trace_id,
                status=status,
                error=error,
                attributes=attributes or {},
            )
        )


__all__ = ["span", "current_span_id"]
