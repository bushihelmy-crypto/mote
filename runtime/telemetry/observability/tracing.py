"""TracingSubscriber — backend-agnostic trace export as a telemetry handler.

The whole tracing tree rides one mechanism: every span and every LLM generation
arrives as a telemetry event carrying *explicit* IDs (``span_id`` / ``parent_span_id``
/ ``trace_id`` / ``request_id``). This subscriber rebuilds the parent→child tree
from those IDs alone — never from any backend's ambient context — and drives a
pluggable :class:`TracerBackend`. Adding OpenTelemetry/Phoenix/etc. later is a
new backend, never a spine change.

Event → backend mapping:

* :class:`SpanStartEvent`  -> ``backend.start_span`` (parent threaded by handle),
  stored by ``span_id``. The ``trace_steps`` knob is applied *here* at
  the exporter boundary: when off, non-root spans (``parent_span_id is not None``)
  are skipped — root spans + generations still export.
* :class:`SpanEndEvent`    -> pop + ``backend.end_span`` (no-op if unknown).
* :class:`ModelAttemptStartedEvent` -> ``backend.start_generation`` (child of
  the span named by ``parent_span_id``), stored by ``attempt_id``.
* :class:`ModelAttemptFinishedEvent` -> pop + update success/error attributes +
  ``end_generation``.

Every backend call is best-effort (try/except → debug log): observability must
never break a turn, and a backend that's uninstalled / drifting degrades to a
silent no-op.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Protocol

from mote.contracts.events.model import ModelAttemptFinishedEvent, ModelAttemptStartedEvent
from mote.contracts.events.telemetry import SpanEndEvent, SpanStartEvent
from mote.runtime.telemetry.logging import logger


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

    priority = 85

    def __init__(self, backend: TracerBackend, *, trace_steps: bool = False):
        self._backend = backend
        self._trace_steps = trace_steps
        #: span_id -> backend span handle.
        self._spans: Dict[str, Any] = {}
        #: request_id -> backend generation handle.
        self._gens: Dict[str, Any] = {}

    async def handle(self, event) -> None:
        try:
            if isinstance(event, SpanStartEvent):
                self._start_span(event)
            elif isinstance(event, SpanEndEvent):
                self._end_span(event)
            elif isinstance(event, ModelAttemptStartedEvent):
                self._start_generation(event)
            elif isinstance(event, ModelAttemptFinishedEvent):
                self._finish_generation(event)
        except Exception as exc:  # noqa: BLE001 — tracing must never break a turn
            logger.debug(f"TracingSubscriber: failed on {getattr(event, 'name', '?')}: {exc}")

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
        self._backend.end_span(handle, status=event.status, error=event.error, attributes=event.attributes)

    # ------------------------------------------------------------ generations
    def _start_generation(self, event: ModelAttemptStartedEvent) -> None:
        # Cap the map defensively: an unmatched request (provider error before a
        # response/error is emitted) would otherwise leak a handle. Bound it.
        if len(self._gens) > 256:
            self._gens.clear()
        handle = self._backend.start_generation(
            request_id=event.attempt_id,
            parent_handle=self._spans.get(event.parent_span_id) if event.parent_span_id else None,
            trace_id=event.trace_id,
            model=event.model,
            input=event.input,
            metadata={
                "provider": event.provider,
                "model_call_id": event.model_call_id,
                "attempt_id": event.attempt_id,
                "endpoint_id": event.endpoint_id,
                "credential_slot_id": event.credential_slot_id,
                "resume_generation": event.resume_generation,
            },
        )
        self._gens[event.attempt_id] = handle

    def _finish_generation(self, event: ModelAttemptFinishedEvent) -> None:
        handle = self._gens.pop(event.attempt_id, None)
        if handle is None:
            return
        metadata = {"cost_usd": event.cost_usd, "latency_ms": event.latency_ms}
        if event.state == "succeeded":
            self._backend.update_generation(
                handle,
                output=event.output,
                usage=event.usage,
                metadata=metadata,
            )
        else:
            self._backend.update_generation(
                handle,
                level="ERROR",
                status_message=event.failure_reason or event.state,
                metadata=metadata,
            )
        self._backend.end_generation(handle)

    async def aclose(self) -> None:
        """End any unmatched handles before the shared exporter is flushed."""

        for handle in reversed(tuple(self._gens.values())):
            self._backend.end_generation(handle)
        self._gens.clear()
        for handle in reversed(tuple(self._spans.values())):
            self._backend.end_span(handle, status="cancelled", error="shutdown", attributes={})
        self._spans.clear()


__all__ = ["TracerBackend", "TracingSubscriber"]
