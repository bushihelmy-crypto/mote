#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for DeferredToolIndexContextSource (the tool-search menu, per turn).

The source renders the compact menu of deferred (hidden) tools — name +
one-line description — so the model knows what it can ``SearchTools`` to reveal.
It is duck-typed over a single ``get_index`` callable (never imports the
executor) and ephemeral (``save_to_context`` False). Reveal-awareness lives in
the *getter* (the wiring passes ``include_revealed=False``), so the source is a
pure renderer of whatever the callable yields — a revealed tool drops out of the
menu. This costs no cache churn: the block rides the reminder tail after the
cache breakpoint (never in the cached prefix).
"""
from __future__ import annotations

import asyncio

from mote.contracts.ports.conversation.turn_context import EphemeralContextSource
from mote.runtime.context.turn import DeferredToolIndexContextSource


def run(coro):
    return asyncio.run(coro)


INDEX = {
    "ConvertImage": "Convert an image between formats.",
    "QueryDatabase": "Run a read-only SQL query.",
}


def test_is_ephemeral_context_source():
    src = DeferredToolIndexContextSource(get_index=lambda: {})
    assert isinstance(src, EphemeralContextSource)
    # Ephemeral: re-injected each turn, never persisted into history.
    assert src.save_to_context is False


def test_renders_all_deferred_tools():
    src = DeferredToolIndexContextSource(get_index=lambda: INDEX)
    out = run(src.render())
    assert out is not None
    assert "ConvertImage: Convert an image between formats." in out
    assert "QueryDatabase: Run a read-only SQL query." in out
    assert "SearchTools" in out  # tells the model how to reveal


def test_none_when_empty():
    src = DeferredToolIndexContextSource(get_index=lambda: {})
    assert run(src.render()) is None


def test_byte_stable_across_turns():
    src = DeferredToolIndexContextSource(get_index=lambda: INDEX)
    first = run(src.render())
    second = run(src.render())
    assert first == second


def test_shrinks_as_getter_drops_revealed():
    # The source is a pure renderer of the getter's output: reveal-filtering
    # lives in the getter (wired with include_revealed=False), so once a tool is
    # revealed it disappears from what the getter yields → out of the menu.
    menu = dict(INDEX)
    src = DeferredToolIndexContextSource(get_index=lambda: menu)
    before = run(src.render())
    assert "QueryDatabase" in before
    # Simulate the getter dropping a revealed tool.
    del menu["QueryDatabase"]
    after = run(src.render())
    assert "QueryDatabase" not in after
    assert "ConvertImage" in after
