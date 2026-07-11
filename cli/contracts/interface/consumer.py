#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``Consumer`` — one host's delivery adapter for a single projected protocol.

A consumer is, structurally, an ``ObservationSubscriber`` that consumes a
*projected* event (``ViewEvent`` for human hosts, ``ServerNotification`` for
machine hosts) and **delivers** it — to a terminal, a websocket, a Lark card, a
JSON-lines stdout. It can also **eat** an event it can't render: a consumer
never has to handle every event kind.

This is a LEAF interface module: at runtime it imports only ``typing`` (the
``Capabilities`` reference is a ``TYPE_CHECKING``-only annotation), so it can be
imported from any host without risking a cycle. The eat/dispatch *plumbing* that
implements this contract lives separately in
:class:`mote.cli.contracts.base.consumer.BaseConsumer`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from mote.cli.contracts.view.capabilities import Capabilities


@runtime_checkable
class Consumer(Protocol):
    """One host's delivery adapter for a single projected protocol.

    ``capabilities`` drives the per-consumer ``CapabilityAdapter`` upstream
    (a non-streaming consumer never sees raw deltas). ``handle`` delivers (or
    eats) one event; ``aclose`` releases any transport the consumer holds.
    """

    capabilities: "Capabilities"

    async def handle(self, ev: Any) -> None:
        ...

    async def aclose(self) -> None:
        ...


__all__ = ["Consumer"]
