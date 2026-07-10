#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``BaseConsumer`` — the eat/dispatch plumbing behind the ``Consumer`` contract.

A subclass overrides only the event kinds it cares about: ``handle(ev)``
dispatches to a method named ``on_<ev.kind>`` (e.g. ``on_tool_call_started``); a
missing method routes to ``on_unhandled`` which eats by default. Handlers may be
sync **or** async — a returned awaitable is awaited — so a rich-console consumer
(sync prints) and a webhook consumer (async I/O) share one base without ceremony.

Lives in the local ``common`` layer because every host's consumer subclasses it;
the structural contract it implements is
:class:`metagpt.cli.common.interface.consumer.Consumer`.
"""

from __future__ import annotations

import inspect
from typing import Any

from metagpt.cli.common.view.capabilities import Capabilities


class BaseConsumer:
    """Dispatch + eat helpers; subclasses override only the kinds they render.

    The observation-plane attributes (``priority`` / ``delivery``) let a consumer
    be subscribed to the bus *directly* too, but the normal path is via a
    projector that owns the consumer and routes folded events into ``handle``.
    """

    #: Default: a consumer renders nothing special until it declares otherwise.
    capabilities: Capabilities = Capabilities()

    # Observation-plane hints (used only if subscribed to a bus directly).
    priority: int = 100
    delivery: str = "mirror"

    async def handle(self, ev: Any) -> None:
        """Dispatch one projected event to ``on_<kind>`` (eat if absent).

        The async path: a handler may be sync or async — an awaitable return is
        awaited. Used for the bulk of events the bus delivers asynchronously.
        """
        handler = self._handler_for(ev)
        if handler is None:
            self.on_unhandled(ev)
            return
        result = handler(ev)
        if inspect.isawaitable(result):
            await result

    def handle_sync(self, ev: Any) -> None:
        """Synchronous dispatch for events the bus delivers via ``emit_sync``.

        Stream deltas / task progress arrive on the sync path; a consumer whose
        handlers are sync (rich prints, a JSON write) renders them here. If a
        handler is async it cannot be awaited on this path — it is skipped to
        avoid a dangling coroutine — so async-only consumers should rely on the
        ``handle`` path (they declare ``streaming=False`` to be fed whole blocks).
        """
        handler = self._handler_for(ev)
        if handler is None:
            self.on_unhandled(ev)
            return
        result = handler(ev)
        if inspect.isawaitable(result):  # async handler on the sync path — close it
            result.close()

    def _handler_for(self, ev: Any):
        kind = getattr(ev, "kind", None)
        return getattr(self, f"on_{kind}", None) if kind else None

    def on_unhandled(self, ev: Any) -> None:
        """Eat an event this consumer can't render. Override to log/forward."""
        return None

    async def aclose(self) -> None:
        """Release any held transport. Default no-op."""
        return None


__all__ = ["BaseConsumer"]
