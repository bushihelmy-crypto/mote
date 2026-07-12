#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``SurfaceDriver`` — glue a :class:`TranscriptReducer` to a :class:`RenderSurface`.

Itself a :class:`~mote.cli.contracts.base.consumer.BaseConsumer`, so a host wires
``SurfaceDriver(surface=…)`` into ``build_app(consumer_objs=[…])`` exactly like
any other consumer — the projector / :class:`CapabilityAdapter` path is
untouched. It declares ``TERMINAL_CAPS`` (streaming on) so it receives raw
per-token deltas.

It defines **no** ``on_<kind>`` handlers, so every event — the async block/tool
path (``handle``) and the sync delta/progress path (``handle_sync``) — routes to
``on_unhandled``, which folds the event through the reducer and lands each op on
the surface. Reducer folds and surface methods are all synchronous (a terminal
prints inline; a Textual surface mutates widgets on the UI pump it already runs
on), so both dispatch paths work without ceremony.
"""

from __future__ import annotations

from typing import Any, Optional

from mote.cli.consumers.transcript.reducer import TranscriptReducer
from mote.cli.consumers.transcript.surface import RenderSurface
from mote.cli.contracts.base import BaseConsumer
from mote.cli.contracts.view import TERMINAL_CAPS, Capabilities


def apply_ops(reducer: TranscriptReducer, surface: RenderSurface, ev: Any) -> None:
    """Fold one ``ViewEvent`` through the reducer and land each op on the surface.

    The single host-blind dispatch step: ``op.kind`` is the surface method name,
    so one ``getattr`` routes with no lookup table to drift. Both rich hosts call
    this — the terminal via :class:`SurfaceDriver` (inline, on the consumer thread)
    and the Textual app inline on its UI pump — so "how an op lands on a surface"
    is written exactly once.
    """
    for op in reducer.feed(ev):
        getattr(surface, op.kind)(*op.surface_args())


class SurfaceDriver(BaseConsumer):
    """Reducer + surface, packaged as a streaming ``BaseConsumer``."""

    capabilities: Capabilities = TERMINAL_CAPS

    def __init__(self, surface: RenderSurface, reducer: Optional[TranscriptReducer] = None) -> None:
        self._surface = surface
        self._reducer = reducer if reducer is not None else TranscriptReducer()

    def on_unhandled(self, ev: Any) -> None:
        # No ``on_<kind>`` methods exist, so every event (async or sync path)
        # lands here and is folded through the single reducer.
        apply_ops(self._reducer, self._surface, ev)

    async def aclose(self) -> None:
        self._surface.close()


__all__ = ["SurfaceDriver", "apply_ops"]
