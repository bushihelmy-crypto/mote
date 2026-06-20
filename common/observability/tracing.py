"""TracingSubscriber — backend-agnostic trace export as an event-bus consumer.

The whole tracing tree rides one mechanism: every span and every LLM generation
arrives as a bus event carrying *explicit* IDs (``span_id`` / ``parent_span_id``
/ ``trace_id`` / ``request_id``). This subscriber rebuilds the parent→child tree
from those IDs alone — never from any backend's ambient context — and drives a
pluggable :class:`TracerBackend`. Adding OpenTelemetry/Phoenix/etc. later is a
new backend, never a spine change.

Event → backend mapping:

* :class:`SpanStartEvent`  -> ``backend.start_span`` (parent threaded by handle),
  stored by ``span_id``. The legacy ``trace_steps`` knob is preserved *here* at
  the exporter boundary: when off, non-root spans (``parent_span_id is not None``)
  are skipped — root spans + generations still export.
* :class:`SpanEndEvent`    -> pop + ``backend.end_span`` (no-op if unknown).
* :class:`LLMRequestEvent` -> ``backend.start_generation`` (child of the span
  named by ``parent_span_id``), stored by ``request_id`` (map capped at 256).
* :class:`LLMResponseEvent`-> pop + ``update_generation`` (output/usage/cost) +
  ``end_generation``.
* :class:`LLMErrorEvent`   -> pop + ``update_generation`` (ERROR) + ``end_generation``.

Every backend call is best-effort (try/except → debug log): observability must
never break a turn, and a backend that's uninstalled / drifting degrades to a
silent no-op.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Protocol

from metagpt.common.events.types import (
    LLMErrorEvent,
    LLMRequestEvent,
    LLMResponseEvent,
    SpanEndEvent,
    SpanStartEvent,
)
from metagpt.common.hook.types import HookOutcome
from metagpt.common.logs import logger


class TracerBackend(Protocol):
    """A pluggable trace exporter, driven by explicit IDs threaded as handles.

    Implementations translate span/generation lifecycle calls into a concrete
    backend (langfuse today). Parent linkage is passed as an opaque ``handle``
    the backend returned from a prior ``start_*`` — never resolved from ambient
    context — so the spine stays backend-agnostic.
    """

    def start_span(
        self,
        *,
        span_id: str,
        parent_handle: Any,
        trace_id: str,
        label: str,
        attributes: dict,
    ) -> Any:
        ...

    def end_span(self, handle: Any, *, status: str, error: str, attributes: dict) -> None:
        ...

    def start_generation(
        self,
        *,
        request_id: str,
        parent_handle: Any,
        trace_id: str,
        model: str,
        input: Any,
        metadata: dict,
    ) -> Any:
        ...

    def update_generation(
        self,
        handle: Any,
        *,
        output: Any = None,
        usage: Any = None,
        metadata: Optional[dict] = None,
        level: Optional[str] = None,
        status_message: Optional[str] = None,
    ) -> None:
        ...

    def end_generation(self, handle: Any) -> None:
        ...


class TracingSubscriber:
    """Rebuilds the trace tree from explicit IDs and drives a :class:`TracerBackend`."""

    #: After the recorder (80) — purely cosmetic since it folds nothing; an
    #: external mirror reads cleanly as "trace what finally happened".
    priority: int = 85

    def __init__(self, backend: TracerBackend, *, trace_steps: bool = False):
        self._backend = backend
        self._trace_steps = trace_steps
        #: span_id -> backend span handle.
        self._spans: Dict[str, Any] = {}
        #: request_id -> backend generation handle.
        self._gens: Dict[str, Any] = {}

    async def handle(self, event) -> Optional[HookOutcome]:
        try:
            if isinstance(event, SpanStartEvent):
                self._start_span(event)
            elif isinstance(event, SpanEndEvent):
                self._end_span(event)
            elif isinstance(event, LLMRequestEvent):
                self._start_generation(event)
            elif isinstance(event, LLMResponseEvent):
                self._finish_generation(event)
            elif isinstance(event, LLMErrorEvent):
                self._fail_generation(event)
        except Exception as exc:  # noqa: BLE001 — tracing must never break a turn
            logger.debug(f"TracingSubscriber: failed on {getattr(event, 'name', '?')}: {exc}")
        return None

    # ------------------------------------------------------------------ spans
    def _start_span(self, event: SpanStartEvent) -> None:
        # trace_steps knob at the exporter boundary: skip non-root step spans
        # when step tracing is off (root span + generations still export).
        if not self._trace_steps and event.parent_span_id is not None:
            return
        handle = self._backend.start_span(
            span_id=event.span_id,
            parent_handle=self._spans.get(event.parent_span_id) if event.parent_span_id else None,
            trace_id=event.trace_id,
            label=event.label,
            attributes=event.attributes,
        )
        self._spans[event.span_id] = handle

    def _end_span(self, event: SpanEndEvent) -> None:
        handle = self._spans.pop(event.span_id, None)
        if handle is None:
            return
        self._backend.end_span(
            handle, status=event.status, error=event.error, attributes=event.attributes
        )

    # ------------------------------------------------------------ generations
    def _start_generation(self, event: LLMRequestEvent) -> None:
        # Cap the map defensively: an unmatched request (provider error before a
        # response/error is emitted) would otherwise leak a handle. Bound it.
        if len(self._gens) > 256:
            self._gens.clear()
        handle = self._backend.start_generation(
            request_id=event.request_id,
            parent_handle=self._spans.get(event.parent_span_id) if event.parent_span_id else None,
            trace_id=event.trace_id,
            model=event.model,
            input=event.messages,
            metadata={"provider": event.provider, "request_id": event.request_id},
        )
        self._gens[event.request_id] = handle

    def _finish_generation(self, event: LLMResponseEvent) -> None:
        handle = self._gens.pop(event.request_id, None)
        if handle is None:
            return
        self._backend.update_generation(
            handle,
            output=event.content or None,
            usage=event.usage,
            metadata={"cost_usd": event.cost_usd, "latency_ms": event.latency_ms},
        )
        self._backend.end_generation(handle)

    def _fail_generation(self, event: LLMErrorEvent) -> None:
        handle = self._gens.pop(event.request_id, None)
        if handle is None:
            return
        self._backend.update_generation(
            handle,
            level="ERROR",
            status_message=f"{event.error_type}: {event.error}",
            metadata={"latency_ms": event.latency_ms},
        )
        self._backend.end_generation(handle)


__all__ = ["TracerBackend", "TracingSubscriber"]
