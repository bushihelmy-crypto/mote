#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ToolCatalogContextSource (the volatile per-turn tool catalog).

The source delivers the hot-reloadable tool *list* (built-in / MCP / pipeline
JSON schemas) in the per-turn ``<system-reminder>`` under the XML protocol only.
The static usage guide stays in the system prompt (``${tool_usage_guide}``). The
first turn emits the whole catalog; later turns emit only newly-appeared tools
(tracked by ``_sent_names``); a ``PostCompactEvent`` resets the frontier so the
next turn re-sends everything. Under native tool-use the channel reports
``wants_tool_catalog()`` False and the source self-suppresses. It is duck-typed
over ``get_executor`` / ``get_channel`` callables so it never imports the
executor or the Role.
"""
from __future__ import annotations

import asyncio
import json

from mote.common.events import PostCompactEvent, ToolsChangedEvent
from mote.common.interface import EphemeralContextSource
from mote.context.turn_context import ToolCatalogContextSource


def run(coro):
    return asyncio.run(coro)


class _FakeChannel:
    def __init__(self, wants_catalog=True):
        self._wants = wants_catalog

    def wants_tool_catalog(self) -> bool:
        return self._wants


class _FakeExecutor:
    """Mimics ToolExecutor's three schema accessors over plain dicts."""

    def __init__(self, builtin=None, mcp=None, pipeline=None):
        self._builtin = builtin or {}
        self._mcp = mcp or {}
        self._pipeline = pipeline or {}

    def get_tool_schemas(self):
        return self._builtin

    def get_mcp_tool_schemas(self):
        return self._mcp

    def get_pipeline_tool_schemas(self):
        return self._pipeline


def _source(executor, channel=None):
    channel = channel if channel is not None else _FakeChannel()
    return ToolCatalogContextSource(
        get_executor=lambda: executor,
        get_channel=lambda: channel,
    )


class TestProtocol:
    def test_is_ephemeral_context_source(self):
        assert isinstance(_source(_FakeExecutor()), EphemeralContextSource)

    def test_save_to_context_true(self):
        # Persisted to history once per turn (the full first-turn listing is
        # cache-hittable), not request-only ephemeral.
        assert _source(_FakeExecutor()).save_to_context is True

    def test_priority_leads_reminder(self):
        assert _source(_FakeExecutor()).priority == 5


class TestSilent:
    def test_native_channel_suppresses(self):
        # wants_tool_catalog() False → native passes tools via API tools= param.
        exe = _FakeExecutor(builtin={"Read": {"desc": "read"}})
        src = _source(exe, channel=_FakeChannel(wants_catalog=False))
        assert run(src.render()) is None

    def test_none_channel_silent(self):
        src = ToolCatalogContextSource(
            get_executor=lambda: _FakeExecutor(builtin={"Read": {}}),
            get_channel=lambda: None,
        )
        assert run(src.render()) is None

    def test_none_executor_silent(self):
        src = ToolCatalogContextSource(
            get_executor=lambda: None,
            get_channel=lambda: _FakeChannel(),
        )
        assert run(src.render()) is None

    def test_empty_catalog_silent(self):
        assert run(_source(_FakeExecutor()).render()) is None


class TestFirstTurn:
    def test_emits_full_catalog(self):
        exe = _FakeExecutor(
            builtin={"Read": {"desc": "read"}},
            mcp={"github:get_me": {"desc": "me"}},
            pipeline={"deploy": {"desc": "ship"}},
        )
        out = run(_source(exe).render())
        assert out is not None
        assert "# Available Commands" in out
        assert "# MCP Tools" in out
        assert "# Pipeline Tools" in out
        assert "Read" in out and "github:get_me" in out and "deploy" in out

    def test_no_usage_guide_in_output(self):
        # The static usage principle lives in the system prompt, NOT here.
        exe = _FakeExecutor(builtin={"Read": {}}, mcp={"gh:x": {}})
        out = run(_source(exe).render())
        assert "# Using tools" not in out
        assert "may fail" not in out

    def test_omits_empty_categories(self):
        exe = _FakeExecutor(builtin={"Read": {}})
        out = run(_source(exe).render())
        assert "# Available Commands" in out
        assert "# MCP Tools" not in out
        assert "# Pipeline Tools" not in out

    def test_marks_sent_names(self):
        exe = _FakeExecutor(builtin={"Read": {}}, mcp={"gh:x": {}})
        src = _source(exe)
        run(src.render())
        assert src._sent_names == {"Read", "gh:x"}

    def test_json_serializes_schemas(self):
        exe = _FakeExecutor(builtin={"Read": {"desc": "read a file"}})
        out = run(_source(exe).render())
        # The schema dict is emitted as valid JSON after the header.
        body = out.split("# Available Commands\n", 1)[1]
        assert json.loads(body) == {"Read": {"desc": "read a file"}}


class TestIncremental:
    def test_second_turn_no_change_silent(self):
        exe = _FakeExecutor(builtin={"Read": {}})
        src = _source(exe)
        run(src.render())  # first turn: full
        assert run(src.render()) is None  # nothing new → silent

    def test_new_tool_emitted_incrementally(self):
        exe = _FakeExecutor(builtin={"Read": {}})
        src = _source(exe)
        run(src.render())  # sends Read
        exe._mcp = {"gh:x": {"desc": "new mcp"}}  # hot-reload connects a server
        out = run(src.render())
        assert out is not None
        assert "# New tools available" in out
        assert "gh:x" in out
        assert "Read" not in out  # only the delta
        assert "# MCP Tools" in out
        assert "# Available Commands" not in out  # no new built-ins
        assert src._sent_names == {"Read", "gh:x"}

    def test_removed_tool_not_reannounced(self):
        exe = _FakeExecutor(builtin={"Read": {}, "Write": {}})
        src = _source(exe)
        run(src.render())  # sends Read, Write
        exe._builtin = {"Read": {}}  # Write gated off
        assert run(src.render()) is None  # removals are not re-announced


class TestPostCompactReset:
    def test_post_compact_resets_frontier_and_resends_full(self):
        exe = _FakeExecutor(builtin={"Read": {}}, mcp={"gh:x": {}})
        src = _source(exe)
        run(src.render())  # first turn: full
        assert run(src.render()) is None  # steady: nothing new

        run(src.handle(PostCompactEvent()))  # compaction condensed history
        assert src._sent_names == set()

        out = run(src.render())  # next turn re-sends the WHOLE catalog
        assert out is not None
        assert "# Available Commands" in out
        assert "Read" in out and "gh:x" in out
        assert "# New tools available" not in out  # full render, not a delta

    def test_handle_ignores_other_events(self):
        exe = _FakeExecutor(builtin={"Read": {}})
        src = _source(exe)
        run(src.render())
        run(src.handle(object()))  # unrelated event → no reset
        assert src._sent_names == {"Read"}


class TestToolsChangedRefresh:
    def test_removed_name_drops_from_frontier(self):
        exe = _FakeExecutor(builtin={"Read": {}, "Write": {}})
        src = _source(exe)
        run(src.render())  # sends Read, Write
        run(src.handle(ToolsChangedEvent(removed=["Write"])))
        # Only the removed name leaves the frontier; the rest stays announced.
        assert src._sent_names == {"Read"}

    def test_reregistered_tool_is_reannounced(self):
        # A tool de-registered then re-registered must be re-announced — without
        # the frontier drop it would sit silently in _sent_names forever.
        exe = _FakeExecutor(builtin={"Read": {}, "Write": {}})
        src = _source(exe)
        run(src.render())  # sends Read, Write
        exe._builtin = {"Read": {}}  # Write de-registered
        run(src.handle(ToolsChangedEvent(removed=["Write"])))
        exe._builtin = {"Read": {}, "Write": {"desc": "back"}}  # Write re-registered
        out = run(src.render())
        assert out is not None
        assert "# New tools available" in out
        assert "Write" in out
        assert "Read" not in out  # only the delta

    def test_unknown_removed_name_is_harmless(self):
        exe = _FakeExecutor(builtin={"Read": {}})
        src = _source(exe)
        run(src.render())
        run(src.handle(ToolsChangedEvent(removed=["NeverSent"])))
        assert src._sent_names == {"Read"}
