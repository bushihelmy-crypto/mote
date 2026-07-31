#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for :class:`AguiConsumer` — the ViewEvent → AG-UI SSE output half.

The consumer is transport-free: it folds each projected ``ViewEvent`` through
the pure ``_wire/agui`` mapper and pushes the resulting dicts to an injected
async ``sink``. A list-appending fake sink lets us assert the exact wire-frame
sequence without a socket — the headline being that a streaming text block folds
to ``TEXT_MESSAGE_START → …CONTENT → …END`` and a tool call to
``TOOL_CALL_START (+ARGS) → …END → …RESULT``.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from mote.product.interfaces.agui.consumer import AGUI_CAPS, AguiConsumer
from mote.product.presentation.events import events as ev


class RecordingSink:
    """An async wire sink that just records every AG-UI event dict it's handed."""

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []

    async def __call__(self, event: Dict[str, Any]) -> None:
        self.events.append(event)


def _types(sink: RecordingSink) -> List[str]:
    return [e["type"] for e in sink.events]


def make_consumer() -> tuple[AguiConsumer, RecordingSink]:
    sink = RecordingSink()
    consumer = AguiConsumer(thread_id="t1", run_id="r1", sink=sink)
    return consumer, sink


# --------------------------------------------------------------------------
# Capability declaration
# --------------------------------------------------------------------------
def test_declares_streaming_caps():
    # AG-UI is a token-streaming protocol → the adapter must NOT buffer deltas.
    assert AGUI_CAPS.streaming is True
    assert AGUI_CAPS.markdown is True
    assert AGUI_CAPS.interactive is True
    assert AGUI_CAPS.images is True


# --------------------------------------------------------------------------
# Streaming text block → START / CONTENT / END
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_streaming_text_block_folds_to_message_triple():
    consumer, sink = make_consumer()
    await consumer.handle(ev.MessageBlockStarted(role="assistant"))
    await consumer.handle(ev.MessageBlockDelta(text="Hel"))
    await consumer.handle(ev.MessageBlockDelta(text="lo"))
    await consumer.handle(ev.MessageBlockCompleted(role="assistant", markdown="Hello", streamed=True))
    assert _types(sink) == [
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
    ]
    # Same messageId threads the whole block.
    ids = {e["messageId"] for e in sink.events}
    assert len(ids) == 1
    assert [e for e in sink.events if e["type"] == "TEXT_MESSAGE_CONTENT"][0]["delta"] == "Hel"


@pytest.mark.asyncio
async def test_non_streamed_completed_synthesizes_full_triple():
    consumer, sink = make_consumer()
    # A block that never streamed → the mapper synthesizes START+CONTENT+END.
    await consumer.handle(ev.MessageBlockCompleted(role="assistant", markdown="whole", streamed=False))
    assert _types(sink) == ["TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END"]
    assert sink.events[1]["delta"] == "whole"


# --------------------------------------------------------------------------
# Tool call → START (+ARGS) / END / RESULT
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_tool_call_started_emits_start_and_args():
    consumer, sink = make_consumer()
    await consumer.handle(ev.ToolCallStarted(tool_name="Bash", headline="ls -la", tool_use_id="tc-1"))
    assert _types(sink) == ["TOOL_CALL_START", "TOOL_CALL_ARGS"]
    assert sink.events[0]["toolCallId"] == "tc-1"
    assert sink.events[0]["toolCallName"] == "Bash"
    assert sink.events[1]["delta"] == "ls -la"


@pytest.mark.asyncio
async def test_tool_call_completed_emits_end_and_result():
    consumer, sink = make_consumer()
    await consumer.handle(ev.ToolCallCompleted(ok=True, summary="done", tool_use_id="tc-1"))
    assert _types(sink) == ["TOOL_CALL_END", "TOOL_CALL_RESULT"]
    assert sink.events[0]["toolCallId"] == "tc-1"
    assert sink.events[1]["content"] == "done"


# --------------------------------------------------------------------------
# Unknown / display-only kinds → nothing
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_unknown_kind_emits_nothing():
    consumer, sink = make_consumer()

    class Bogus:
        kind = "not_a_real_kind"

    await consumer.handle(Bogus())
    assert sink.events == []


# --------------------------------------------------------------------------
# Lifecycle frames + robustness
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_emit_lifecycle_writes_through_sink():
    from mote.product.interfaces.agui import wire as agui

    consumer, sink = make_consumer()
    await consumer.emit_lifecycle(agui.run_started(consumer.wire_state))
    await consumer.emit_lifecycle(agui.run_finished(consumer.wire_state))
    assert _types(sink) == ["RUN_STARTED", "RUN_FINISHED"]
    assert sink.events[0]["threadId"] == "t1"
    assert sink.events[0]["runId"] == "r1"


@pytest.mark.asyncio
async def test_closed_consumer_stops_emitting():
    consumer, sink = make_consumer()
    await consumer.aclose()
    await consumer.handle(ev.MessageBlockCompleted(markdown="ignored", streamed=False))
    assert sink.events == []


@pytest.mark.asyncio
async def test_sink_exception_is_swallowed():
    async def boom(_event):
        raise RuntimeError("dead socket")

    consumer = AguiConsumer(thread_id="t", run_id="r", sink=boom)
    # A failing sink must not propagate — a dead client never crashes the turn.
    await consumer.handle(ev.MessageBlockCompleted(markdown="x", streamed=False))


@pytest.mark.asyncio
async def test_sync_path_is_noop():
    consumer, sink = make_consumer()
    consumer.handle_sync(ev.MessageBlockDelta(text="ignored"))
    assert sink.events == []
