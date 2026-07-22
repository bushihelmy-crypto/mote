#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for :class:`AcpConsumer` — the ViewEvent → ACP ``session/update`` half.

The consumer is transport-free: it folds each projected ``ViewEvent`` through
the pure ``_wire/acp`` mapper and pushes each resulting ``update`` dict to an
injected async ``sink`` (the server binds that sink to a ``session/update``
notification writer). A list-appending fake sink lets us assert the exact
``sessionUpdate`` sequence without a pipe.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from mote.cli.consumers._wire import acp
from mote.cli.consumers.acp.consumer import ACP_CAPS, AcpConsumer
from mote.cli.contracts.view import events as ev


class RecordingSink:
    """An async sink recording every ACP ``update`` dict it's handed."""

    def __init__(self) -> None:
        self.updates: List[Dict[str, Any]] = []

    async def __call__(self, update: Dict[str, Any]) -> None:
        self.updates.append(update)


def make_consumer() -> tuple[AcpConsumer, RecordingSink]:
    sink = RecordingSink()
    return AcpConsumer(session_id="sess-1", sink=sink), sink


def _kinds(sink: RecordingSink) -> List[str]:
    return [u["sessionUpdate"] for u in sink.updates]


# ── capabilities ─────────────────────────────────────────────────────────────
def test_declares_streaming_no_images():
    assert ACP_CAPS.streaming is True
    assert ACP_CAPS.interactive is True
    # ACP prompt media is off (mapper degrades images to text pointers)
    assert ACP_CAPS.images is False


# ── streaming text → agent_message_chunk×N ───────────────────────────────────
@pytest.mark.asyncio
async def test_streaming_text_folds_to_agent_chunks():
    consumer, sink = make_consumer()
    await consumer.handle(ev.MessageBlockStarted(role="assistant"))
    await consumer.handle(ev.MessageBlockDelta(text="Hel"))
    await consumer.handle(ev.MessageBlockDelta(text="lo"))
    await consumer.handle(ev.MessageBlockCompleted(role="assistant", markdown="Hello", streamed=True))
    # started + completed emit nothing; two deltas each emit one agent chunk
    assert _kinds(sink) == [acp.AGENT_MESSAGE_CHUNK, acp.AGENT_MESSAGE_CHUNK]
    ids = {u["messageId"] for u in sink.updates}
    assert len(ids) == 1  # one messageId threads the block


@pytest.mark.asyncio
async def test_tool_lifecycle_start_then_update():
    consumer, sink = make_consumer()
    await consumer.handle(ev.ToolCallStarted(tool_name="Bash", headline="ls", tool_use_id="tc-1"))
    await consumer.handle(ev.ToolCallCompleted(ok=True, summary="ok", tool_use_id="tc-1"))
    assert _kinds(sink) == [acp.TOOL_CALL, acp.TOOL_CALL_UPDATE]
    assert sink.updates[0]["toolCallId"] == "tc-1"
    assert sink.updates[1]["status"] == acp.STATUS_COMPLETED


# ── unknown / display-only kinds → nothing ───────────────────────────────────
@pytest.mark.asyncio
async def test_unknown_kind_emits_nothing():
    consumer, sink = make_consumer()

    class Bogus:
        kind = "not_a_real_kind"

    await consumer.handle(Bogus())
    assert sink.updates == []


# ── robustness ───────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_closed_consumer_stops_emitting():
    consumer, sink = make_consumer()
    await consumer.aclose()
    await consumer.handle(ev.MessageBlockCompleted(markdown="ignored", streamed=False))
    assert sink.updates == []


@pytest.mark.asyncio
async def test_sink_exception_is_swallowed():
    async def boom(_update):
        raise RuntimeError("dead pipe")

    consumer = AcpConsumer(session_id="s", sink=boom)
    # a dead client sink must never crash the turn
    await consumer.handle(ev.MessageBlockCompleted(markdown="x", streamed=False))


@pytest.mark.asyncio
async def test_sync_path_is_noop():
    consumer, sink = make_consumer()
    result = consumer.handle_sync(ev.MessageBlockDelta(text="ignored"))
    assert result is None
    assert sink.updates == []


@pytest.mark.asyncio
async def test_set_sink_late_binds():
    # A consumer built without a sink can have one bound later (the server's
    # per-prompt wiring path); updates before binding are simply dropped.
    consumer = AcpConsumer(session_id="s")
    await consumer.handle(ev.MessageBlockCompleted(markdown="before", streamed=False))
    sink = RecordingSink()
    consumer.set_sink(sink)
    await consumer.handle(ev.MessageBlockCompleted(markdown="after", streamed=False))
    assert any("after" in u.get("content", {}).get("text", "") for u in sink.updates)
