#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for :class:`mote.context.visibility.ContextVisibility`.

The service answers one question — "is the most-recent tool result derived from
resource X still present (real content, not a cleared placeholder)?" — by reading
the live message history. These tests drive it against hand-built histories that
mimic the four states a reconstructable read can be in: present, folded, erased,
and never-read.
"""
from __future__ import annotations

from mote.common.const.context import TOOL_RESULT_CLEARED_MESSAGE
from mote.common.schema import AIMessage, ToolMessage, UserMessage
from mote.context import ContextVisibility


def _tool_result(content: str, *, tool_call_id: str, resource_path: str) -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=tool_call_id, resource_path=resource_path)


def _vis(messages) -> ContextVisibility:
    return ContextVisibility(lambda: messages)


class TestIsResourceVisible:
    def test_present_real_content_is_visible(self):
        msgs = [_tool_result("     1→hi", tool_call_id="c1", resource_path="/f/a.txt")]
        assert _vis(msgs).is_resource_visible("/f/a.txt") is True

    def test_folded_content_is_not_visible(self):
        msgs = [_tool_result(TOOL_RESULT_CLEARED_MESSAGE, tool_call_id="c1", resource_path="/f/a.txt")]
        assert _vis(msgs).is_resource_visible("/f/a.txt") is False

    def test_erased_result_is_not_visible(self):
        # Erase removes the tool_result message entirely — nothing tags the path.
        msgs = [UserMessage(content="read a.txt"), AIMessage(content="done")]
        assert _vis(msgs).is_resource_visible("/f/a.txt") is False

    def test_never_read_is_not_visible(self):
        msgs = [_tool_result("other", tool_call_id="c1", resource_path="/f/other.txt")]
        assert _vis(msgs).is_resource_visible("/f/a.txt") is False

    def test_empty_path_is_not_visible(self):
        msgs = [_tool_result("hi", tool_call_id="c1", resource_path="/f/a.txt")]
        assert _vis(msgs).is_resource_visible("") is False

    def test_latest_result_wins_present_after_folded(self):
        # An older folded read followed by a fresh present one => visible.
        msgs = [
            _tool_result(TOOL_RESULT_CLEARED_MESSAGE, tool_call_id="c1", resource_path="/f/a.txt"),
            _tool_result("     1→fresh", tool_call_id="c2", resource_path="/f/a.txt"),
        ]
        assert _vis(msgs).is_resource_visible("/f/a.txt") is True

    def test_latest_result_wins_folded_after_present(self):
        # An older present read followed by a newer folded one => NOT visible:
        # the dedup cache stands in for the latest view, which is now cleared.
        msgs = [
            _tool_result("     1→old", tool_call_id="c1", resource_path="/f/a.txt"),
            _tool_result(TOOL_RESULT_CLEARED_MESSAGE, tool_call_id="c2", resource_path="/f/a.txt"),
        ]
        assert _vis(msgs).is_resource_visible("/f/a.txt") is False

    def test_untagged_tool_result_ignored(self):
        # A tool result with no resource_path (e.g. a dedup stub) must not count
        # as this file's latest result — otherwise it would mask a folded read.
        msgs = [
            _tool_result("     1→real", tool_call_id="c1", resource_path="/f/a.txt"),
        ]
        stub = ToolMessage(content="File unchanged", tool_call_id="c2")  # untagged
        msgs.append(stub)
        assert _vis(msgs).is_resource_visible("/f/a.txt") is True

    def test_provider_is_read_live(self):
        # The provider is called fresh each query, so a later fold is observed.
        msgs = [_tool_result("     1→hi", tool_call_id="c1", resource_path="/f/a.txt")]
        vis = _vis(msgs)
        assert vis.is_resource_visible("/f/a.txt") is True
        msgs[0].content = TOOL_RESULT_CLEARED_MESSAGE  # simulate an in-place fold
        assert vis.is_resource_visible("/f/a.txt") is False

    def test_non_tool_message_with_matching_metadata_ignored(self):
        # Defensive: only role=="tool" messages are considered.
        m = AIMessage(content="hi")
        m.metadata["tool_result_resource_path"] = "/f/a.txt"
        m.metadata["tool_call_id"] = "c1"
        assert _vis([m]).is_resource_visible("/f/a.txt") is False
