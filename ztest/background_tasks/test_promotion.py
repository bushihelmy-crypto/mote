#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for :func:`mote.tasks.promotion.auto_background`.

Covers the three outcomes: the coroutine finishes inside the foreground window
(value returned directly), it raises inside the window (exception re-raised),
and it overruns the window (adopted into the pool, ``BgTaskResult`` returned and
the task later completing through the pool).
"""

from __future__ import annotations

import asyncio

import pytest

from mote.orchestration.background_tasks import BackgroundTaskStatus, BgTaskResult, auto_background

from .conftest import boom, echo, gated, wait_started


class TestForegroundCompletion:
    @pytest.mark.asyncio
    async def test_returns_value_when_fast(self, pool):
        out = await auto_background(echo("fast"), pool, "quick", foreground_timeout=1)
        assert out == "fast"
        assert pool.has_pending() is False

    @pytest.mark.asyncio
    async def test_reraises_exception_from_foreground(self, pool):
        with pytest.raises(ValueError, match="boom"):
            await auto_background(boom(), pool, "explode", foreground_timeout=1)
        assert pool.has_pending() is False


class TestPromotion:
    @pytest.mark.asyncio
    async def test_promotes_to_background_when_slow(self, pool, msg_buffer):
        release = asyncio.Event()
        out = await auto_background(
            gated(release, "late"), pool, "slow-cmd", foreground_timeout=0.02, task_timeout=None
        )
        # Promoted: a BgTaskResult comes back immediately, task still pending.
        assert isinstance(out, BgTaskResult)
        assert out.command_name == "slow-cmd"
        assert "background" in out.result
        assert pool.has_pending() is True
        assert pool.pending_ids == ["bg_1"]

        # Let it finish through the pool and verify the completion path.
        release.set()
        await pool.wait_all()
        meta = pool.get_task_info("bg_1")
        assert meta.status == BackgroundTaskStatus.SUCCESS
        assert meta.result == "late"
        assert not msg_buffer.empty()  # completion notification was pushed

    @pytest.mark.asyncio
    async def test_zero_foreground_timeout_promotes_immediately(self, pool):
        release = asyncio.Event()
        out = await auto_background(gated(release, "v"), pool, "cmd", foreground_timeout=0, task_timeout=None)
        assert isinstance(out, BgTaskResult)
        release.set()
        await pool.wait_all()
