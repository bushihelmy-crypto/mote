#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for :class:`SpawnUsageGate` — the fleet token/cost ceiling on the plane.

The gate reads the fleet's LIVE cumulative spend through injected reader closures
(so it stays decoupled from the cost layer) and folds a ``deny`` once the configured
USD-cost or total-token budget is reached. This gate is READ-ONLY: it mutates
nothing, it only measures. It is fail-closed so a crash denies the spawn.
"""
from __future__ import annotations

import asyncio

import pytest

from mote.common.events import EventBus, LLMStreamDeltaEvent, PreAgentSpawnEvent
from mote.common.interface.event_subscriber import FAIL_CLOSED
from mote.environment.spawn_usage_gate import SpawnUsageGate


def _spawn(depth: int = 1) -> PreAgentSpawnEvent:
    return PreAgentSpawnEvent(parent_path="/root", child_depth=depth, max_depth=None)


@pytest.mark.asyncio
async def test_no_caps_never_denies():
    gate = SpawnUsageGate()
    for _ in range(100):
        assert await gate.handle_control(_spawn()) is None


@pytest.mark.asyncio
async def test_cost_cap_denies_when_reached():
    spent = 0.0
    gate = SpawnUsageGate(max_cost_usd=1.00, cost_reader=lambda: spent)
    # under budget → admits
    assert await gate.handle_control(_spawn()) is None
    spent = 1.00
    out = await gate.handle_control(_spawn())
    assert out is not None and out.denied
    assert out.is_blocking
    assert "cost budget" in out.reason


@pytest.mark.asyncio
async def test_cost_cap_denies_when_exceeded():
    gate = SpawnUsageGate(max_cost_usd=0.50, cost_reader=lambda: 2.34)
    out = await gate.handle_control(_spawn())
    assert out is not None and out.denied
    assert "$2.34 spent" in out.reason


@pytest.mark.asyncio
async def test_token_cap_denies_when_reached():
    used = 0
    gate = SpawnUsageGate(max_total_tokens=1000, tokens_reader=lambda: used)
    assert await gate.handle_control(_spawn()) is None
    used = 1000
    out = await gate.handle_control(_spawn())
    assert out is not None and out.denied
    assert "token budget (1000)" in out.reason
    assert "1000 used" in out.reason


@pytest.mark.asyncio
async def test_cap_without_reader_is_inert():
    # A cap with no reader has nothing to measure → never denies.
    gate = SpawnUsageGate(max_cost_usd=0.01, max_total_tokens=1)
    assert await gate.handle_control(_spawn()) is None


@pytest.mark.asyncio
async def test_reader_without_cap_is_inert():
    # A reader with no cap is never consulted → never denies.
    gate = SpawnUsageGate(cost_reader=lambda: 999.0, tokens_reader=lambda: 10**9)
    assert await gate.handle_control(_spawn()) is None


@pytest.mark.asyncio
async def test_cost_denies_before_tokens():
    # Both over budget → cost is checked first (deny reason names cost).
    gate = SpawnUsageGate(
        max_cost_usd=1.0,
        max_total_tokens=100,
        cost_reader=lambda: 5.0,
        tokens_reader=lambda: 500,
    )
    out = await gate.handle_control(_spawn())
    assert out is not None and out.denied
    assert "cost budget" in out.reason


@pytest.mark.asyncio
async def test_ignores_non_spawn_events():
    gate = SpawnUsageGate(max_cost_usd=0.0, cost_reader=lambda: 999.0)
    out = await gate.handle_control(LLMStreamDeltaEvent(token="x"))
    assert out is None


def test_is_fail_closed():
    assert SpawnUsageGate.fail_mode == FAIL_CLOSED


def test_on_failure_denies():
    out = SpawnUsageGate.on_failure("boom")
    assert out.denied
    assert out.reason == "boom"


@pytest.mark.asyncio
async def test_folds_deny_on_the_bus():
    """Wired on a bus, an over-budget spawn folds a blocking deny the emitter can
    translate into AgentLimitReached; within budget → None."""
    bus = EventBus()
    bus.subscribe(SpawnUsageGate(max_total_tokens=10, tokens_reader=lambda: 50))
    over = await bus.emit(_spawn())
    assert over is not None
    assert over.is_blocking

    bus2 = EventBus()
    bus2.subscribe(SpawnUsageGate(max_total_tokens=10, tokens_reader=lambda: 0))
    ok = await bus2.emit(_spawn())
    assert ok is None


@pytest.mark.asyncio
async def test_live_reader_reflects_growing_spend():
    """The gate re-reads the closure each spawn, so a fleet crossing budget between
    spawns flips from admit to deny with no gate re-configuration."""
    box = {"tokens": 0}
    gate = SpawnUsageGate(max_total_tokens=1000, tokens_reader=lambda: box["tokens"])
    assert await gate.handle_control(_spawn()) is None
    box["tokens"] = 999
    assert await gate.handle_control(_spawn()) is None
    box["tokens"] = 1001
    assert (await gate.handle_control(_spawn())) is not None


@pytest.mark.asyncio
async def test_concurrent_reads_are_consistent():
    """Read-only gate: racing spawns at the boundary all see the same live spend
    and all deny (no admit slips through, nothing to over-count)."""
    gate = SpawnUsageGate(max_cost_usd=1.0, cost_reader=lambda: 1.0)
    outcomes = await asyncio.gather(*(gate.handle_control(_spawn()) for _ in range(20)))
    assert all(o is not None and o.denied for o in outcomes)
