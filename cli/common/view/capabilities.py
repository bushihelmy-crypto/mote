#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``Capabilities`` + ``CapabilityAdapter`` — declarative capability bits.

A consumer **declares** what it can render (``streaming`` / ``markdown`` /
``syntax_highlight`` / ...); the :class:`CapabilityAdapter` **downgrades** the
single ``ViewEvent`` stream to fit. This is how one ``ViewEvent`` stream feeds
both a live token-streaming TUI *and* a batch chat card — without a single
``if consumer == "feishu"`` branch anywhere (ARCHITECTURE §2.4).

The headline downgrade: a non-streaming consumer can't show per-token deltas, so
the adapter **buffers** ``MessageBlockDelta`` and synthesizes a single
``MessageBlockCompleted`` at the block boundary (preferring the block's own
completed markdown when it arrives, falling back to the accumulated buffer).

Lives in the local ``common`` layer: capability declaration + downgrade is a
cross-host concern — every host declares its ``Capabilities`` and the same
adapter shapes the shared ``ViewEvent`` stream for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from metagpt.cli.common.view.events import (
    MessageBlockCompleted,
    MessageBlockDelta,
    MessageBlockStarted,
    ReasoningDelta,
    ToolCallStarted,
    ViewEvent,
)


@dataclass(frozen=True)
class Capabilities:
    """What a consumer's host can render. Pure data, no behavior."""

    streaming: bool = False
    markdown: bool = False
    syntax_highlight: bool = False
    interactive: bool = False
    rich_panels: bool = False
    images: bool = False


# A rich terminal: everything on.
TERMINAL_CAPS = Capabilities(
    streaming=True,
    markdown=True,
    syntax_highlight=True,
    interactive=True,
    rich_panels=True,
    images=False,
)

# A plain JSON-lines / structured sink: it gets every event verbatim (it does
# its own shaping), so we model it as fully capable to avoid lossy downgrade.
STRUCTURED_CAPS = Capabilities(streaming=True, markdown=True, interactive=False)


class CapabilityAdapter:
    """Per-consumer, stateful downgrade of a ``ViewEvent`` stream.

    One adapter instance per consumer (it carries per-block buffer state).
    ``adapt(ev) -> list[ViewEvent]`` maps one upstream event to zero-or-more
    downstream events the consumer can actually render.
    """

    def __init__(self, caps: Capabilities):
        self._caps = caps
        self._buffer: List[str] = []
        self._buffering = False

    @property
    def capabilities(self) -> Capabilities:
        return self._caps

    def adapt(self, ev: ViewEvent) -> List[ViewEvent]:
        # Streaming-capable consumers see the stream untouched.
        if self._caps.streaming:
            return [ev]

        # Non-streaming: swallow per-token deltas, accumulate, emit one block.
        if isinstance(ev, MessageBlockStarted):
            self._buffer = []
            self._buffering = True
            return []  # a non-streaming consumer has no "block opened" concept
        if isinstance(ev, (MessageBlockDelta, ReasoningDelta)):
            self._buffer.append(ev.text)
            return []
        if isinstance(ev, MessageBlockCompleted):
            self._buffering = False
            markdown = ev.markdown or "".join(self._buffer)
            self._buffer = []
            # Re-stamp as not-streamed: the consumer must render it fresh.
            return [MessageBlockCompleted(role=ev.role, markdown=markdown, streamed=False)]

        # A tool panel with a body but no syntax highlighting → strip the lexer
        # so the consumer renders the body as plain text.
        if isinstance(ev, ToolCallStarted) and not self._caps.syntax_highlight:
            return [ev.model_copy(update={"lexer": None})]

        return [ev]


__all__ = ["Capabilities", "CapabilityAdapter", "TERMINAL_CAPS", "STRUCTURED_CAPS"]
