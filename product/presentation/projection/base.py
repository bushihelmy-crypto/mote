#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``BaseProjector`` — fans one ``AgentEvent`` fold out to many consumers (§2.2.1).

One projector instance handles the unified telemetry stream as a
structural handler and routes folded ``ViewEvent`` instances out to *many*
consumers, each behind its own :class:`CapabilityAdapter` (so a non-streaming
consumer gets a downgraded stream without the projector knowing anything
consumer-specific).

The *concrete* fold is injected (any :class:`~mote.product.presentation.projection.protocol.Projector`
— anything with ``project(event) -> list``), so this reusable plumbing never
imports a host-specific projector upward. The human host injects ``ViewProjector``
(:mod:`mote.product.presentation.projection`); the machine host injects ``AppServerProjector``.

Telemetry delivers ``LLMStreamDeltaEvent`` / ``TaskProgressEvent`` **synchronously**
(``emit_sync`` → ``handle_sync``) and everything else **asynchronously**
(``observe`` → ``handle``). This implements *both* entry points and folds
identically through the injected ``project`` — only the downstream dispatch differs.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from mote.product.presentation.consumer_protocol import Consumer, SyncConsumer
from mote.product.presentation.events.capabilities import CapabilityAdapter
from mote.product.presentation.events.events import ViewEvent
from mote.product.presentation.input_events import PresentationInputEvent
from mote.product.presentation.projection.protocol import Projector
from mote.runtime.control.lifecycle import LifecyclePhase, LifecycleStack


class PresentationConsumer(Consumer[ViewEvent], SyncConsumer[ViewEvent], Protocol):
    """Consumer shape required by the dual-path presentation fan-out."""


class BaseProjector:
    """Subscribes to the ``AgentEvent`` spine and routes folded events to consumers.

    Registers on the observation plane; implements ``handle`` (async, for the
    bulk of events) and ``handle_sync`` (for ``emit_sync`` deltas/progress). The concrete fold
    is injected via ``projector`` (a :class:`Projector`); there is no default so
    the base stays host-agnostic.

    It is a structural handler and never participates in control outcomes.
    """

    def __init__(
        self,
        consumers: Iterable[PresentationConsumer] | None = None,
        *,
        projector: Projector[PresentationInputEvent, ViewEvent],
    ) -> None:
        self._projector = projector
        # Each consumer is paired with its own adapter carrying its capabilities.
        self._consumers: list[PresentationConsumer] = []
        self._adapters: list[CapabilityAdapter] = []
        self._lifecycle = LifecycleStack()
        for consumer in consumers or []:
            self.add_consumer(consumer)

    # -- consumer registry --------------------------------------------------

    def add_consumer(self, consumer: PresentationConsumer) -> None:
        """Register a consumer; its declared ``capabilities`` drive downgrade."""
        caps = consumer.capabilities
        self._consumers.append(consumer)
        self._adapters.append(CapabilityAdapter(caps))
        self._lifecycle.register_close(
            f"consumer:{type(consumer).__module__}.{type(consumer).__qualname__}:{id(consumer)}",
            consumer.aclose,
            phase=LifecyclePhase.CLOSE_RESOURCES,
        )

    @property
    def consumers(self) -> list[PresentationConsumer]:
        return list(self._consumers)

    # -- telemetry handler entry points -------------------------------------

    async def handle(self, event: PresentationInputEvent) -> None:
        """Phase-2 async observer: fold then dispatch to each consumer (async)."""
        for view_event in self._projector.project(event):
            await self._emit(view_event)

    def handle_sync(self, event: PresentationInputEvent) -> None:
        """Sync observer for ``emit_sync`` events (stream deltas / task progress)."""
        for view_event in self._projector.project(event):
            self._emit_sync(view_event)

    # -- direct delivery (command notices, etc.) ----------------------------

    async def deliver(self, view_event: ViewEvent) -> None:
        """Push a pre-built ``ViewEvent`` (e.g. a command ``Notice``) to consumers.

        Bypasses the fold (the event is already a ``ViewEvent``, not an
        ``AgentEvent``) but still runs each consumer's capability adapter — so a
        command's output renders correctly on every host, not just stdout (§2.7).
        """
        await self._emit(view_event)

    def deliver_sync(self, view_event: ViewEvent) -> None:
        """Synchronous counterpart of :meth:`deliver` (sync command handlers)."""
        self._emit_sync(view_event)

    async def aclose(self) -> None:
        """Close every consumer transport owned by this projector."""

        await self._lifecycle.aclose()
        self._consumers.clear()
        self._adapters.clear()

    # -- dispatch -----------------------------------------------------------

    async def _emit(self, view_event: ViewEvent) -> None:
        for consumer, adapter in zip(self._consumers, self._adapters):
            for shaped in adapter.adapt(view_event):
                await consumer.handle(shaped)

    def _emit_sync(self, view_event: ViewEvent) -> None:
        for consumer, adapter in zip(self._consumers, self._adapters):
            for shaped in adapter.adapt(view_event):
                consumer.handle_sync(shaped)


__all__ = ["BaseProjector"]
