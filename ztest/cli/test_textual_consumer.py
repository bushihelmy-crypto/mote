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

from mote.contracts.tool.identity import ToolAttemptOrdinal, ToolInvocationId, ToolInvocationIdentity
from mote.product.interfaces.textual.app import ViewEventMessage
from mote.product.interfaces.textual.consumer import TextualConsumer
from mote.product.interfaces.textual.surface import TextualSurface
from mote.product.presentation.events import (
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


class _FakeApp:
    """Records every ``post_message`` call (thread-safe funnel stand-in)."""

    def __init__(self) -> None:
        self.posted: list = []

    def post_message(self, message) -> None:
        self.posted.append(message)


def _identity(value: str) -> ToolInvocationIdentity:
    return ToolInvocationIdentity(
        ToolInvocationId(value),
        ToolAttemptOrdinal(1),
        "test-definition",
        1,
        "sha256:test",
        "test-owner",
        "test-run",
    )


_ALL_EVENTS = [
    MessageBlockStarted(),
    MessageBlockDelta(text="hi"),
    MessageBlockCompleted(markdown="done", streamed=True),
    ReasoningDelta(text="think"),
    ToolCallStarted(identity=_identity("tu-1"), tool_name="Read"),
    ToolCallCompleted(identity=_identity("tu-1"), tool_name="Read", summary="ok"),
    MediaBlock(identity=_identity("media-1"), media_kind="image", ref="/tmp/x.png"),
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


def test_capabilities_enable_provisional_rollback():
    from mote.product.presentation.events import TEXTUAL_CAPS

    assert TextualConsumer.capabilities is TEXTUAL_CAPS
    assert TextualConsumer.capabilities.provisional_rollback is True


def test_textual_surface_rollback_removes_only_open_provisional_block():
    class Block:
        removed = False

        def remove(self):
            self.removed = True

    block = Block()
    app = type("App", (), {"_open_block": block})()

    TextualSurface(app).rollback_block()

    assert block.removed is True
    assert app._open_block is None


def test_textual_surface_empty_delta_does_not_create_placeholder_block():
    class App:
        def _ensure_block(self):
            raise AssertionError("empty delta must not create a transcript block")

    TextualSurface(App()).append_delta("", reasoning=False)


def test_textual_surface_empty_notice_does_not_mount_placeholder_row():
    class App:
        def _close_block(self):
            raise AssertionError("empty notice must not mutate the transcript")

        def _mount(self, widget):
            raise AssertionError("empty notice must not mount a transcript row")

    assert TextualSurface(App()).render_notice(Notice(text="")) is None
