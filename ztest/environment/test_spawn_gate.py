#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for :class:`SpawnGate` — the agent-spawn depth veto on the control plane.

The gate reads only the resolved lineage facts a ``PreAgentSpawnEvent`` carries
(child depth + effective ceiling) and folds a ``deny`` when the child would
exceed the limit. It is fail-closed so a crash denies the spawn.
"""
from __future__ import annotations

import pytest

from mote.common.events import EventBus, LLMStreamDeltaEvent, PreAgentSpawnEvent
from mote.common.interface.event_subscriber import FAIL_CLOSED
from mote.environment.spawn_gate import SpawnGate


@pytest.mark.asyncio
async def test_denies_when_child_depth_exceeds_ceiling():
    gate = SpawnGate()
    out = await gate.handle_control(PreAgentSpawnEvent(parent_path="/root/a/b", child_depth=3, max_depth=2))
    assert out is not None
    assert out.denied
    assert out.is_blocking
    assert "depth limit (2)" in out.reason


@pytest.mark.asyncio
async def test_allows_when_within_ceiling():
    gate = SpawnGate()
    out = await gate.handle_control(PreAgentSpawnEvent(parent_path="/root", child_depth=1, max_depth=2))
    assert out is None  # no veto, no outcome contributed


@pytest.mark.asyncio
async def test_allows_at_exact_ceiling():
    # exceeds is strictly greater-than, so depth == max_depth is allowed.
    gate = SpawnGate()
    out = await gate.handle_control(PreAgentSpawnEvent(parent_path="/root/a", child_depth=2, max_depth=2))
    assert out is None


@pytest.mark.asyncio
async def test_no_ceiling_never_denies():
    gate = SpawnGate()
    out = await gate.handle_control(PreAgentSpawnEvent(parent_path="/root", child_depth=99, max_depth=None))
    assert out is None


@pytest.mark.asyncio
async def test_ignores_non_spawn_events():
    gate = SpawnGate()
    out = await gate.handle_control(LLMStreamDeltaEvent(token="x"))
    assert out is None


def test_is_fail_closed():
    assert SpawnGate.fail_mode == FAIL_CLOSED


@pytest.mark.asyncio
async def test_folds_deny_on_the_bus():
    """Wired on a bus, an over-limit spawn folds a blocking deny the emitter can
    translate into AgentLimitReached."""
    bus = EventBus()
    bus.subscribe(SpawnGate())
    out = await bus.emit(PreAgentSpawnEvent(parent_path="/root/a/b", child_depth=3, max_depth=2))
    assert out is not None
    assert out.is_blocking
    # Within limit → None (no control contribution; bucket ran but no subscriber
    # returned an outcome).
    ok = await bus.emit(PreAgentSpawnEvent(parent_path="/root", child_depth=1, max_depth=2))
    assert ok is None
