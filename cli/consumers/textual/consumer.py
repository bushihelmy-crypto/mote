#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``TextualConsumer`` — bridge the ``ViewEvent`` stream onto the Textual UI thread.

The projector feeds this consumer the same ``ViewEvent`` union every host sees,
but a Textual widget must only ever be mutated on the app's message-pump thread.
So instead of rendering inline (like the terminal consumer), this consumer does
ONE thing: it re-posts every event as a single
:class:`~metagpt.cli.consumers.textual.app.ViewEventMessage` via
``App.post_message`` — which is safe to call from any thread and preserves FIFO
order. A single ``on_view_event_message`` handler on
:class:`~metagpt.cli.consumers.textual.app.MetaGPTApp` then performs the actual
widget mutation on the UI thread (§design C).

Because it overrides :meth:`on_unhandled` and declares **no** ``on_<kind>``
methods, EVERY event kind — streamed deltas (sync path) and whole blocks (async
path) alike — funnels through the one post. It declares ``TERMINAL_CAPS`` so the
projector streams deltas to it (the app coalesces them into an
:class:`AssistantBlock`). It is deliberately **not** registry-registered: it needs
the live ``App`` instance, so ``run_textual`` injects it via ``consumer_objs``.
"""

from __future__ import annotations

from typing import Any

from metagpt.cli.common.base import BaseConsumer
from metagpt.cli.common.view import TERMINAL_CAPS, Capabilities


class TextualConsumer(BaseConsumer):
    """Post every ``ViewEvent`` to the app as a ``ViewEventMessage`` (UI-thread safe)."""

    capabilities: Capabilities = TERMINAL_CAPS

    def __init__(self, app: Any) -> None:
        self._app = app

    def on_unhandled(self, ev: Any) -> None:
        # Late import avoids a module-level cycle (app builds this consumer).
        from metagpt.cli.consumers.textual.app import ViewEventMessage

        self._app.post_message(ViewEventMessage(ev))

    async def aclose(self) -> None:
        return None


__all__ = ["TextualConsumer"]
