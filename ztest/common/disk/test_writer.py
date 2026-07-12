#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the L1 :class:`DiskWriter` — the serial async write queue.

Each test builds an *isolated* ``DiskWriter`` and ``aclose``-s it; we never touch
the process-wide singleton so nothing leaks across tests / loops. The contract
under test: per-key FIFO ordering (a single worker gives a global total order,
the legal stronger guarantee), ``submit`` delivers results/exceptions, ``drain``
is a barrier, ``enqueue`` isolates a failing job, ``aclose`` is idempotent, and
the **sync-fallback** runs work inline when no event loop is running.
"""
from __future__ import annotations

import asyncio

import pytest

from mote.common.disk.writer import DiskWriter


def _boom():
    raise ValueError("intentional write failure")


# ---------------------------------------------------------------------------
# Async path (a real loop + worker)
# ---------------------------------------------------------------------------


def test_enqueue_preserves_fifo_order():
    async def go():
        w = DiskWriter()
        order: list[int] = []
        for i in range(25):
            w.enqueue("stream", lambda i=i: order.append(i))
        await w.drain()
        await w.aclose()
        return order

    assert asyncio.run(go()) == list(range(25))


def test_submit_returns_result():
    async def go():
        w = DiskWriter()
        result = await w.submit("k", lambda: 6 * 7)
        await w.aclose()
        return result

    assert asyncio.run(go()) == 42


def test_submit_propagates_exception():
    async def go():
        w = DiskWriter()
        with pytest.raises(ValueError):
            await w.submit("k", _boom)
        await w.aclose()

    asyncio.run(go())


def test_drain_waits_for_backlog():
    async def go():
        w = DiskWriter()
        done: list[int] = []
        for i in range(10):
            w.enqueue("k", lambda i=i: done.append(i))
        await w.drain()  # barrier: every queued job must have run by now
        result = list(done)
        await w.aclose()
        return result

    assert asyncio.run(go()) == list(range(10))


def test_enqueue_bad_job_does_not_break_queue():
    async def go():
        w = DiskWriter()
        out: list[str] = []
        w.enqueue("k", _boom)  # failing job is logged + skipped
        w.enqueue("k", lambda: out.append("after"))
        await w.drain()
        await w.aclose()
        return out

    assert asyncio.run(go()) == ["after"]


def test_aclose_is_idempotent():
    async def go():
        w = DiskWriter()
        await w.submit("k", lambda: 1)
        await w.aclose()
        await w.aclose()  # second close is a harmless no-op

    asyncio.run(go())


def test_writer_rebinds_to_a_new_loop():
    # The codebase runs many asyncio.run calls, each a fresh loop. A reused
    # writer must rebind its worker to the new loop rather than orphan itself.
    w = DiskWriter()
    out: list[int] = []

    async def first():
        w.enqueue("k", lambda: out.append(1))
        await w.drain()

    async def second():
        w.enqueue("k", lambda: out.append(2))
        await w.drain()

    asyncio.run(first())
    asyncio.run(second())
    assert out == [1, 2]


# ---------------------------------------------------------------------------
# Sync-fallback (no running event loop -> inline execution)
# ---------------------------------------------------------------------------


def test_enqueue_runs_inline_without_loop():
    w = DiskWriter()
    out: list[str] = []
    w.enqueue("k", lambda: out.append("ran"))
    # No loop was running, so the job executed inline before enqueue returned.
    assert out == ["ran"]


def test_enqueue_inline_error_is_isolated():
    w = DiskWriter()
    # A failing inline job is logged, not raised, mirroring the async path.
    w.enqueue("k", _boom)  # must not raise


def test_submit_runs_inline_without_loop():
    w = DiskWriter()
    # With no running loop, submit returns fn() before its first await, so the
    # coroutine completes on the first send (StopIteration carries the result).
    coro = w.submit("k", lambda: 99)
    try:
        coro.send(None)
        result = None
    except StopIteration as stop:
        result = stop.value
    assert result == 99
