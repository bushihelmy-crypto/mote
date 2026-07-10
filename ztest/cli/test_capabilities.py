#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``CapabilityAdapter`` — the declarative per-consumer downgrade.

The headline behavior (ARCHITECTURE §2.4): a streaming consumer sees the stream
verbatim, while a non-streaming consumer has its per-token deltas *buffered* and
synthesized into a single ``MessageBlockCompleted`` at the block boundary. A
non-syntax-highlight consumer gets the tool body lexer stripped. These are the
two downgrades one ``ViewEvent`` stream must survive to feed heterogeneous hosts.
"""

from __future__ import annotations

from metagpt.cli.view import (
    STRUCTURED_CAPS,
    TERMINAL_CAPS,
    CapabilityAdapter,
    Capabilities,
    MessageBlockCompleted,
    MessageBlockDelta,
    MessageBlockStarted,
    Notice,
    ToolCallStarted,
)


def test_streaming_consumer_passes_everything_through():
    ad = CapabilityAdapter(Capabilities(streaming=True))
    started = MessageBlockStarted()
    delta = MessageBlockDelta(text="a")
    assert ad.adapt(started) == [started]
    assert ad.adapt(delta) == [delta]


def test_non_streaming_buffers_deltas_into_one_block():
    ad = CapabilityAdapter(Capabilities(streaming=False))
    # block start is swallowed (no "opened" concept downstream)
    assert ad.adapt(MessageBlockStarted()) == []
    assert ad.adapt(MessageBlockDelta(text="Hel")) == []
    assert ad.adapt(MessageBlockDelta(text="lo")) == []
    out = ad.adapt(MessageBlockCompleted(role="assistant", markdown="", streamed=True))
    assert len(out) == 1
    block = out[0]
    assert isinstance(block, MessageBlockCompleted)
    # accumulated from buffer since completed.markdown was empty
    assert block.markdown == "Hello"
    # re-stamped: a non-streaming consumer must render it fresh
    assert block.streamed is False


def test_non_streaming_prefers_completed_markdown_over_buffer():
    ad = CapabilityAdapter(Capabilities(streaming=False))
    ad.adapt(MessageBlockStarted())
    ad.adapt(MessageBlockDelta(text="partial"))
    out = ad.adapt(MessageBlockCompleted(markdown="full final text", streamed=True))
    assert out[0].markdown == "full final text"


def test_non_streaming_buffer_resets_between_blocks():
    ad = CapabilityAdapter(Capabilities(streaming=False))
    ad.adapt(MessageBlockStarted())
    ad.adapt(MessageBlockDelta(text="first"))
    first = ad.adapt(MessageBlockCompleted(markdown="", streamed=True))
    assert first[0].markdown == "first"
    # second block must not carry the first block's buffer
    ad.adapt(MessageBlockStarted())
    ad.adapt(MessageBlockDelta(text="second"))
    second = ad.adapt(MessageBlockCompleted(markdown="", streamed=True))
    assert second[0].markdown == "second"


def test_no_syntax_highlight_strips_tool_lexer():
    ad = CapabilityAdapter(Capabilities(streaming=False, syntax_highlight=False))
    started = ToolCallStarted(tool_name="Write", headline="a.py", body="print(1)", lexer="python")
    out = ad.adapt(started)
    assert len(out) == 1
    assert out[0].lexer is None
    assert out[0].body == "print(1)"  # body preserved, only lexer dropped


def test_syntax_highlight_keeps_tool_lexer():
    ad = CapabilityAdapter(Capabilities(streaming=True, syntax_highlight=True))
    started = ToolCallStarted(tool_name="Write", body="x=1", lexer="python")
    assert ad.adapt(started)[0].lexer == "python"


def test_unknown_event_passes_through_on_non_streaming():
    ad = CapabilityAdapter(Capabilities(streaming=False))
    notice = Notice(text="hi")
    assert ad.adapt(notice) == [notice]


def test_terminal_caps_are_fully_capable():
    assert TERMINAL_CAPS.streaming
    assert TERMINAL_CAPS.markdown
    assert TERMINAL_CAPS.syntax_highlight
    assert TERMINAL_CAPS.interactive


def test_structured_caps_stream_without_interactive():
    assert STRUCTURED_CAPS.streaming
    assert STRUCTURED_CAPS.interactive is False
