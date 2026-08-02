#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for AgentExecutionLimiter — ported from execution_tests.rs."""

import asyncio

import pytest

from mote.contracts.agent.errors import AgentLimitReached
from mote.orchestration.agents.turn_queue.limiter import AgentExecutionLimiter


def test_execution_guards_count_active_turns():
    limiter = AgentExecutionLimiter()
    limiter.initialize(1)
    limiter.initialize(2)  # second init ignored — ceiling stays 1

    limiter.ensure_capacity()  # first turn fits
    guard = limiter.guard()
    assert limiter.active == 1

    with pytest.raises(AgentLimitReached) as exc:
        limiter.ensure_capacity()
    assert "concurrent Agent turn capacity" in str(exc.value)

    guard.release()
    limiter.ensure_capacity()  # capacity released
    assert limiter.active == 0


def test_uninitialized_limiter_is_unbounded():
    limiter = AgentExecutionLimiter()
    guards = [limiter.guard() for _ in range(5)]
    limiter.ensure_capacity()  # never raises
    assert limiter.active == 5
    for g in guards:
        g.release()


@pytest.mark.parametrize("invalid", (0, -1, True, False, 1.5))
def test_initialize_rejects_invalid_capacity_without_consuming_one_time_init(invalid):
    limiter = AgentExecutionLimiter()

    with pytest.raises(ValueError, match="positive integer"):
        limiter.initialize(invalid)

    limiter.initialize(1)
    assert limiter.max_concurrent_turns() == 1


def test_guard_context_manager_releases():
    limiter = AgentExecutionLimiter()
    limiter.initialize(1)
    with limiter.guard():
        assert limiter.active == 1
        with pytest.raises(AgentLimitReached):
            limiter.ensure_capacity()
    assert limiter.active == 0
    limiter.ensure_capacity()


def test_double_release_is_safe():
    limiter = AgentExecutionLimiter()
    limiter.initialize(1)
    guard = limiter.guard()
    guard.release()
    guard.release()
    assert limiter.active == 0


@pytest.mark.asyncio
async def test_acquire_waits_for_atomic_permit_and_preserves_receipt_identity():
    limiter = AgentExecutionLimiter()
    limiter.initialize(1)
    first = await limiter.acquire()
    waiting = asyncio.create_task(limiter.acquire())
    await asyncio.sleep(0)
    assert not waiting.done()
    assert limiter.active == 1

    first.release()
    second = await waiting
    assert second.receipt.permit_id != first.receipt.permit_id
    assert limiter.active == 1
    second.release()
    assert limiter.active == 0


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_leak_permit():
    limiter = AgentExecutionLimiter()
    limiter.initialize(1)
    held = await limiter.acquire()
    waiting = asyncio.create_task(limiter.acquire())
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    held.release()
    await asyncio.sleep(0)
    assert limiter.active == 0
