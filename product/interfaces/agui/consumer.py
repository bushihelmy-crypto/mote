#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``AguiConsumer`` — ViewEvent → AG-UI SSE frames (Phase 2 output half).

A :class:`BaseConsumer` that folds each projected ``ViewEvent`` through the pure
:mod:`mote.product.interfaces.agui.wire` mapper and hands the resulting AG-UI event
dicts to an injected async ``sink`` (the server binds this to an SSE response
writer). The consumer owns NO socket — it only produces wire dicts and calls the
sink — so it is fully unit-testable with a list-appending fake sink.

One :class:`AguiWireState` lives per consumer instance (one per ``/run`` turn),
minting the stable ``messageId`` / correlating ``toolCallId`` the AG-UI stream
needs. The transport (server) emits the ``RUN_STARTED`` / ``RUN_FINISHED``
lifecycle frames around the turn; this consumer emits everything in between.

Declares ``streaming=True`` (+ markdown / interactive / images): AG-UI is a
token-streaming protocol, so the upstream :class:`CapabilityAdapter` must pass
deltas through untouched rather than buffering them into completed blocks.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Optional

from mote.product.interfaces.agui import wire as agui
from mote.product.presentation.consumer import Sink, SinkConsumer
from mote.product.presentation.events import Capabilities
from mote.product.presentation.events.events import ViewEvent
from mote.product.presentation.wire_types import WireMapping

# A frontend that streams tokens, renders markdown, gates approvals, shows media.
AGUI_CAPS = Capabilities(
    streaming=True,
    markdown=True,
    syntax_highlight=False,  # AG-UI carries plain text/markdown; no lexer channel
    interactive=True,
    rich_panels=False,
    images=True,
)


class AguiConsumer(SinkConsumer):
    """Fold ``ViewEvent``s into AG-UI events and push them to an async sink.

    ``sink`` is the transport's SSE writer (``async def(event_dict) -> None``);
    the consumer never touches the socket itself. ``thread_id`` / ``run_id`` seed
    the per-run correlation state the mapper stamps onto every frame. All the
    sink/emit/close plumbing lives in :class:`SinkConsumer`; this class supplies
    only the pure AG-UI fold plus the transport-minted lifecycle emit.
    """

    capabilities: Capabilities = AGUI_CAPS
    _log_label = "AguiConsumer"

    def __init__(self, *, thread_id: str, run_id: str, sink: Optional[Sink] = None) -> None:
        super().__init__(sink)
        self._state = agui.AguiWireState(thread_id=thread_id, run_id=run_id)

    @property
    def wire_state(self) -> agui.AguiWireState:
        return self._state

    def _fold(self, ev: ViewEvent) -> Iterable[WireMapping]:
        return agui.to_agui_events(ev, self._state)

    async def emit_lifecycle(self, wire_event: WireMapping) -> None:
        """Emit a transport-minted lifecycle frame (RUN_STARTED / RUN_FINISHED).

        The run lifecycle frames aren't folded from a ViewEvent — the server
        mints them via :func:`agui.run_started` / :func:`agui.run_finished` and
        pushes them through here so all writes funnel through one guarded path.
        """
        await self._emit(wire_event)


def build_agui_consumer(config: object = None) -> AguiConsumer:
    """Registry builder — a bare consumer (server rebinds thread/run/sink per turn).

    ``build_consumers`` constructs one at app-wire time with placeholder ids; the
    AG-UI server does not use this path (it constructs a fresh consumer per
    ``/run`` with the real ``threadId``/``runId``). Present so ``agui`` is a
    first-class registered channel name for diagnostics/help.
    """
    return AguiConsumer(thread_id="pending", run_id="pending")


__all__ = ["AguiConsumer", "build_agui_consumer", "AGUI_CAPS", "Sink"]
