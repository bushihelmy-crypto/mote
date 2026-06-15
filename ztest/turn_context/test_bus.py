#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for metagpt.context.turn_context.bus.TurnContextBus.

The bus is a stateless aggregator: it renders each EphemeralContextSource
concurrently, drops empty/failed blocks, and merges the survivors into one
<system-reminder>. Sources are ordered by ``priority`` (ascending).
"""
from __future__ import annotations

import asyncio

from metagpt.context.turn_context import TurnContextBus


def run(coro):
    return asyncio.run(coro)


class FakeSource:
    def __init__(self, name, priority, block, *, raises=False):
        self.name = name
        self.priority = priority
        self._block = block
        self._raises = raises
        self.seen_cwd = "unset"

    async def render(self, *, cwd=None):
        self.seen_cwd = cwd
        if self._raises:
            raise RuntimeError("boom")
        return self._block


class TestTurnContextBus:
    def test_no_sources_returns_empty(self):
        assert run(TurnContextBus([]).collect()) == ""

    def test_all_empty_blocks_returns_empty(self):
        bus = TurnContextBus([FakeSource("a", 10, None), FakeSource("b", 20, "")])
        assert run(bus.collect()) == ""

    def test_single_block_wrapped(self):
        bus = TurnContextBus([FakeSource("a", 10, "hi")])
        assert run(bus.collect()) == "<system-reminder>\nhi\n</system-reminder>"

    def test_blocks_merged_in_priority_order(self):
        # Construct out of priority order; bus must sort ascending.
        bus = TurnContextBus(
            [FakeSource("late", 40, "Z"), FakeSource("early", 10, "A")]
        )
        out = run(bus.collect())
        assert out == "<system-reminder>\nA\n\nZ\n</system-reminder>"

    def test_failing_source_is_skipped_not_fatal(self):
        bus = TurnContextBus(
            [FakeSource("ok", 10, "good"), FakeSource("bad", 20, None, raises=True)]
        )
        out = run(bus.collect())
        assert out == "<system-reminder>\ngood\n</system-reminder>"

    def test_cwd_propagated_to_sources(self):
        src = FakeSource("a", 10, "x")
        bus = TurnContextBus([src])
        run(bus.collect(cwd="/work"))
        assert src.seen_cwd == "/work"

    def test_non_string_block_dropped(self):
        bus = TurnContextBus([FakeSource("a", 10, 123)])
        assert run(bus.collect()) == ""
