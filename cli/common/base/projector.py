#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``BaseProjector`` — fans one ``AgentEvent`` fold out to many consumers (§2.2.1).

One projector instance subscribes to the unified ``AgentEvent`` spine (as an
``ObservationSubscriber``) and routes the folded ``ViewEvent``\\s out to *many*
consumers, each behind its own :class:`CapabilityAdapter` (so a non-streaming
consumer gets a downgraded stream without the projector knowing anything
consumer-specific).

The *concrete* fold is injected (any :class:`~metagpt.cli.common.interface.projector.Projector`
— anything with ``project(event) -> list``), so this reusable plumbing never
imports a host-specific projector upward. The human host injects ``ViewProjector``
(:mod:`metagpt.cli.view`); the machine host injects ``AppServerProjector``.

The bus delivers ``LLMStreamDeltaEvent`` / ``TaskProgressEvent`` **synchronously**
(``emit_sync`` → ``handle_sync``) and everything else **asynchronously**
(``observe`` → ``handle``). This implements *both* entry points and folds
identically through the injected ``project`` — only the downstream dispatch differs.
"""

from __future__ import annotations

from typing import Any, List

from metagpt.cli.common.interface.projector import Projector
from metagpt.cli.common.view.capabilities import CapabilityAdapter, Capabilities


class BaseProjector:
    """Subscribes to the ``AgentEvent`` spine and routes folded events to consumers.

    Registers on the observation plane; implements ``handle`` (async, for the
    bulk of events) and ``handle_sync`` (for ``emit_sync`` deltas/progress). The
    ``priority`` attr orders it among observers (low = early). The concrete fold
    is injected via ``projector`` (a :class:`Projector`); there is no default so
    the base stays host-agnostic.
    """

    priority: int = 50

    def __init__(self, consumers=None, *, projector: Projector) -> None:
        self._projector = projector
        # Each consumer is paired with its own adapter carrying its capabilities.
        self._consumers: List[Any] = []
        self._adapters: List[CapabilityAdapter] = []
        for consumer in consumers or []:
            self.add_consumer(consumer)

    # -- consumer registry --------------------------------------------------

    def add_consumer(self, consumer: Any) -> None:
        """Register a consumer; its declared ``capabilities`` drive downgrade."""
        caps = getattr(consumer, "capabilities", None) or Capabilities()
        self._consumers.append(consumer)
        self._adapters.append(CapabilityAdapter(caps))

    @property
    def consumers(self) -> list:
        return list(self._consumers)

    # -- bus entry points ---------------------------------------------------

    async def handle(self, event: Any) -> None:
        """Phase-2 async observer: fold then dispatch to each consumer (async)."""
        for view_event in self._projector.project(event):
            await self._emit(view_event)

    def handle_sync(self, event: Any) -> None:
        """Sync observer for ``emit_sync`` events (stream deltas / task progress)."""
        for view_event in self._projector.project(event):
            self._emit_sync(view_event)

    # -- direct delivery (command notices, etc.) ----------------------------

    async def deliver(self, view_event: Any) -> None:
        """Push a pre-built ``ViewEvent`` (e.g. a command ``Notice``) to consumers.

        Bypasses the fold (the event is already a ``ViewEvent``, not an
        ``AgentEvent``) but still runs each consumer's capability adapter — so a
        command's output renders correctly on every host, not just stdout (§2.7).
        """
        await self._emit(view_event)

    def deliver_sync(self, view_event: Any) -> None:
        """Synchronous counterpart of :meth:`deliver` (sync command handlers)."""
        self._emit_sync(view_event)

    # -- dispatch -----------------------------------------------------------

    async def _emit(self, view_event: Any) -> None:
        for consumer, adapter in zip(self._consumers, self._adapters):
            for shaped in adapter.adapt(view_event):
                await consumer.handle(shaped)

    def _emit_sync(self, view_event: Any) -> None:
        for consumer, adapter in zip(self._consumers, self._adapters):
            handler = getattr(consumer, "handle_sync", None)
            if handler is None:
                continue
            for shaped in adapter.adapt(view_event):
                handler(shaped)


__all__ = ["BaseProjector"]
