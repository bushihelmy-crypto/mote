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
    # The aggregation tests below exercise the ephemeral bucket via collect(),
    # so the fixture defaults to save_to_context=False (real sources default
    # True). Routing between the two buckets is covered in TestBucketRouting.
    def __init__(self, name, priority, block, *, raises=False, save_to_context=False):
        self.name = name
        self.priority = priority
        self.save_to_context = save_to_context
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


class TestBucketRouting:
    """save_to_context routes sources into two disjoint buckets."""

    def test_persistent_source_goes_to_collect_to_context_not_collect(self):
        src = FakeSource("p", 10, "P", save_to_context=True)
        bus = TurnContextBus([src])
        assert run(bus.collect()) == ""  # not in the ephemeral bucket
        assert run(bus.collect_to_context()) == "<system-reminder>\nP\n</system-reminder>"

    def test_ephemeral_source_goes_to_collect_not_collect_to_context(self):
        src = FakeSource("e", 10, "E", save_to_context=False)
        bus = TurnContextBus([src])
        assert run(bus.collect()) == "<system-reminder>\nE\n</system-reminder>"
        assert run(bus.collect_to_context()) == ""

    def test_buckets_are_disjoint(self):
        bus = TurnContextBus(
            [
                FakeSource("e", 10, "E", save_to_context=False),
                FakeSource("p", 20, "P", save_to_context=True),
            ]
        )
        assert run(bus.collect()) == "<system-reminder>\nE\n</system-reminder>"
        assert run(bus.collect_to_context()) == "<system-reminder>\nP\n</system-reminder>"

    def test_missing_flag_defaults_to_persisted(self):
        class NoFlag:
            name = "x"
            priority = 10

            async def render(self, *, cwd=None):
                return "X"

        bus = TurnContextBus([NoFlag()])
        assert run(bus.collect()) == ""  # default True => persisted, not ephemeral
        assert run(bus.collect_to_context()) == "<system-reminder>\nX\n</system-reminder>"

    def test_collect_to_context_orders_by_priority(self):
        bus = TurnContextBus(
            [
                FakeSource("late", 40, "Z", save_to_context=True),
                FakeSource("early", 10, "A", save_to_context=True),
            ]
        )
        assert run(bus.collect_to_context()) == "<system-reminder>\nA\n\nZ\n</system-reminder>"

    def test_collect_to_context_propagates_cwd(self):
        src = FakeSource("p", 10, "P", save_to_context=True)
        bus = TurnContextBus([src])
        run(bus.collect_to_context(cwd="/work"))
        assert src.seen_cwd == "/work"
