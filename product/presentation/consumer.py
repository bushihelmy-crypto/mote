#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``BaseConsumer`` — the eat/dispatch plumbing behind the ``Consumer`` contract.

A subclass overrides only the event kinds it cares about: ``handle(ev)``
dispatches to a method named ``on_<ev.kind>`` (e.g. ``on_tool_call_started``); a
missing method routes to ``on_unhandled`` which eats by default. Handlers may be
sync **or** async — a returned awaitable is awaited — so a rich-console consumer
(sync prints) and a webhook consumer (async I/O) share one base without ceremony.

Lives in the shared ``contracts`` layer because every host's consumer subclasses it;
the structural contract it implements is
:class:`mote.product.presentation.consumer_protocol.Consumer`.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Awaitable, Callable, Iterable, Optional, Protocol

from mote.product.presentation.events.capabilities import Capabilities
from mote.product.presentation.events.events import ViewEvent
from mote.product.presentation.wire_types import WireMapping
from mote.runtime.telemetry.logging import logger

#: An async sink the transport injects: one wire payload dict → written out (SSE
#: frame / JSON-RPC notification / ...). The transport owns the socket; a
#: :class:`SinkConsumer` only produces payloads and calls this.
WireObject = WireMapping
Sink = Callable[[WireObject], Awaitable[None]]


class ViewEventHandler(Protocol):
    def __call__(self, event: ViewEvent) -> object: ...


class BaseConsumer:
    """Dispatch + eat helpers; subclasses override only the kinds they render."""

    #: Default: a consumer renders nothing special until it declares otherwise.
    capabilities: Capabilities = Capabilities()

    async def handle(self, ev: ViewEvent) -> None:
        """Dispatch one projected event to ``on_<kind>`` (eat if absent).

        The async path: a handler may be sync or async — an awaitable return is
        awaited. Used for the bulk of telemetry events delivered asynchronously.
        """
        handler = self._handler_for(ev)
        if handler is None:
            self.on_unhandled(ev)
            return
        result = handler(ev)
        if inspect.isawaitable(result):
            await result

    def handle_sync(self, ev: ViewEvent) -> None:
        """Synchronous dispatch for telemetry delivered via ``emit_sync``.

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
        if inspect.iscoroutine(result):  # async handler on the sync path — close it
            result.close()

    def _handler_for(self, ev: ViewEvent) -> ViewEventHandler | None:
        candidate = getattr(self, f"on_{ev.kind}", None)
        return candidate if callable(candidate) else None

    def on_unhandled(self, ev: ViewEvent) -> None:
        """Eat an event this consumer can't render. Override to log/forward."""
        return None

    async def aclose(self) -> None:
        """Release any held transport. Default no-op."""
        return None


class SinkConsumer(BaseConsumer):
    """A :class:`BaseConsumer` that folds each ViewEvent into wire payloads and
    pushes them to an injected async ``sink`` — the shared machinery behind every
    streaming network consumer (AG-UI SSE, ACP ``session/update``, ...).

    A subclass supplies only the pure fold via :meth:`_fold` (delegating to its
    ``_wire`` mapper) and its per-session correlation state; this base owns the
    sink binding, the guarded emit (a dead pipe must never crash a turn), and the
    close flag. The consumer touches NO socket itself — the transport binds
    ``sink`` to the real writer — so it stays fully unit-testable with a
    list-appending fake sink.

    The pure mapper already owns per-kind fan-out, so ``handle`` is a thin
    "fold → emit" loop over :meth:`_fold` (no per-``on_<kind>`` methods). The
    sync path is a no-op: sinks are async-only, so a sync-delivered event is
    dropped rather than leaking a coroutine (subclasses declare
    ``streaming=True`` and are fed the async ``handle`` path for text).
    """

    #: Log label for a failed sink write (subclass sets its consumer name).
    _log_label: str = "SinkConsumer"

    def __init__(self, sink: Optional[Sink] = None) -> None:
        self._sink = sink
        self._closed = False

    def set_sink(self, sink: Sink) -> None:
        """Bind (or rebind) the async wire sink — the server calls this once."""
        self._sink = sink

    def _fold(self, ev: ViewEvent) -> Iterable[WireObject]:
        """Map one ViewEvent to zero+ wire payloads (subclass delegates to its
        pure ``_wire`` mapper). Unknown / display-only kinds → empty."""
        raise NotImplementedError

    async def handle(self, ev: ViewEvent) -> None:
        """Fold one projected ViewEvent through the wire table and emit each payload."""
        for payload in self._fold(ev):
            await self._emit(payload)

    def handle_sync(self, ev: ViewEvent) -> None:
        """No-op: sinks are async-only, so sync-delivered events are dropped."""
        return None

    async def _emit(self, payload: WireObject) -> None:
        if self._sink is None or self._closed:
            return
        try:
            await self._sink(payload)
        except Exception as exc:  # noqa: BLE001 — a dead pipe must not crash the turn
            logger.warning(f"{self._log_label}: sink write failed: {exc}")

    async def aclose(self) -> None:
        self._closed = True


__all__ = ["BaseConsumer", "SinkConsumer", "Sink"]
