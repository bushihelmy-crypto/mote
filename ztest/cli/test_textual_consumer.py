#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``TextualConsumer`` posts EVERY ``ViewEvent`` as one ``ViewEventMessage`` (§C).

The consumer declares no ``on_<kind>`` methods, so both the async (``handle``) and
sync (``handle_sync``) dispatch paths fall through to ``on_unhandled`` — which is
the single funnel that re-posts the event to the app. These tests assert that
funnel with a fake app that just records what ``post_message`` received.
"""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

from metagpt.cli.common.view import (
    ApprovalRequested,
    ErrorRaised,
    MediaBlock,
    MessageBlockCompleted,
    MessageBlockDelta,
    MessageBlockStarted,
    Notice,
    QuestionAsked,
    ReasoningDelta,
    SessionListShown,
    TaskProgress,
    ToolCallCompleted,
    ToolCallStarted,
    UsageUpdated,
)
from metagpt.cli.consumers.textual.app import ViewEventMessage
from metagpt.cli.consumers.textual.consumer import TextualConsumer


class _FakeApp:
    """Records every ``post_message`` call (thread-safe funnel stand-in)."""

    def __init__(self) -> None:
        self.posted: list = []

    def post_message(self, message) -> None:
        self.posted.append(message)


_ALL_EVENTS = [
    MessageBlockStarted(),
    MessageBlockDelta(text="hi"),
    MessageBlockCompleted(markdown="done", streamed=True),
    ReasoningDelta(text="think"),
    ToolCallStarted(tool_name="Read", tool_use_id="tu-1"),
    ToolCallCompleted(tool_name="Read", tool_use_id="tu-1", summary="ok"),
    MediaBlock(media_kind="image", ref="/tmp/x.png"),
    TaskProgress(stage="build", status="running"),
    Notice(text="note", level="info"),
    ErrorRaised(text="boom"),
    QuestionAsked(question="pick?", options=["a", "b"]),
    ApprovalRequested(tool_name="Bash", action="rm -rf", approval_id="ap-1"),
    UsageUpdated(total_tokens=42, model="gpt-4"),
    SessionListShown(),
]


@pytest.mark.parametrize("ev", _ALL_EVENTS, ids=lambda e: e.kind)
def test_on_unhandled_posts_view_event_message(ev):
    app = _FakeApp()
    consumer = TextualConsumer(app)
    consumer.on_unhandled(ev)
    assert len(app.posted) == 1
    msg = app.posted[0]
    assert isinstance(msg, ViewEventMessage)
    assert msg.event is ev


@pytest.mark.asyncio
@pytest.mark.parametrize("ev", _ALL_EVENTS, ids=lambda e: e.kind)
async def test_async_handle_routes_to_post(ev):
    app = _FakeApp()
    consumer = TextualConsumer(app)
    await consumer.handle(ev)
    assert len(app.posted) == 1
    assert app.posted[0].event is ev


@pytest.mark.parametrize("ev", _ALL_EVENTS, ids=lambda e: e.kind)
def test_sync_handle_routes_to_post(ev):
    app = _FakeApp()
    consumer = TextualConsumer(app)
    consumer.handle_sync(ev)
    assert len(app.posted) == 1
    assert app.posted[0].event is ev


def test_capabilities_are_terminal_caps():
    from metagpt.cli.common.view import TERMINAL_CAPS

    assert TextualConsumer.capabilities is TERMINAL_CAPS
