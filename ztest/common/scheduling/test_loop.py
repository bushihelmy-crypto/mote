#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for :class:`mote.runtime.scheduling.PeriodicLoop`.

Covers: ticking until stopped, sync and async ticks, best-effort error
swallowing, self-stop via a ``False`` return, ``sleep_first`` ordering,
idempotent ``start``, and ``is_running`` lifecycle. Tiny intervals keep it fast.
"""
from __future__ import annotations

import asyncio

import pytest

from mote.runtime.scheduling import PeriodicLoop


async def _wait_for(predicate, timeout=2.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return False


@pytest.mark.asyncio
async def test_ticks_until_stopped():
    count = {"n": 0}

    async def tick():
        count["n"] += 1

    loop = PeriodicLoop(0.005, tick)
    loop.start()
    assert await _wait_for(lambda: count["n"] >= 3)
    await loop.stop()
    settled = count["n"]
    await asyncio.sleep(0.03)
    assert count["n"] == settled  # no ticks after stop


@pytest.mark.asyncio
async def test_sync_tick_supported():
    count = {"n": 0}

    def tick():
        count["n"] += 1

    loop = PeriodicLoop(0.005, tick)
    loop.start()
    assert await _wait_for(lambda: count["n"] >= 2)
    await loop.stop()


@pytest.mark.asyncio
async def test_exception_is_swallowed_and_loop_continues():
    count = {"n": 0}

    async def tick():
        count["n"] += 1
        raise RuntimeError("boom")

    loop = PeriodicLoop(0.005, tick)
    loop.start()
    assert await _wait_for(lambda: count["n"] >= 3)  # kept ticking despite raises
    assert loop.is_running()
    await loop.stop()


@pytest.mark.asyncio
async def test_self_stop_on_false():
    count = {"n": 0}

    async def tick():
        count["n"] += 1
        return False  # stop after the first tick

    loop = PeriodicLoop(0.005, tick)
    loop.start()
    assert await _wait_for(lambda: not loop.is_running())
    assert count["n"] == 1


@pytest.mark.asyncio
async def test_sleep_first_delays_first_tick():
    count = {"n": 0}

    async def tick():
        count["n"] += 1

    loop = PeriodicLoop(0.05, tick, sleep_first=True)
    loop.start()
    await asyncio.sleep(0.01)  # well within the first interval
    assert count["n"] == 0  # slept before first tick
    await loop.stop()


@pytest.mark.asyncio
async def test_no_sleep_first_ticks_immediately():
    count = {"n": 0}

    async def tick():
        count["n"] += 1

    loop = PeriodicLoop(0.05, tick, sleep_first=False)
    loop.start()
    assert await _wait_for(lambda: count["n"] >= 1, timeout=0.04)
    await loop.stop()


@pytest.mark.asyncio
async def test_start_is_idempotent():
    async def tick():
        await asyncio.sleep(0.001)

    loop = PeriodicLoop(0.005, tick)
    loop.start()
    first = loop._task
    loop.start()  # no-op while running
    assert loop._task is first
    await loop.stop()


@pytest.mark.asyncio
async def test_is_running_lifecycle():
    async def tick():
        await asyncio.sleep(0.001)

    loop = PeriodicLoop(0.005, tick)
    assert loop.is_running() is False
    loop.start()
    assert loop.is_running() is True
    await loop.stop()
    assert loop.is_running() is False


@pytest.mark.asyncio
async def test_cancel_is_sync_and_idempotent():
    async def tick():
        await asyncio.sleep(0.001)

    loop = PeriodicLoop(0.005, tick)
    loop.start()
    loop.cancel()
    loop.cancel()  # safe second call
    assert await _wait_for(lambda: not loop.is_running())
