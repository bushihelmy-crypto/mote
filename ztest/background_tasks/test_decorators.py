#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for :mod:`mote.tasks.decorators`.

Covers the ``require_bg_complete`` gate (no-wait fast path, wait-until-empty path, and the
``None``-pool path). ``ThoughtReporter`` is patched so the gate never touches
the network.
"""

from __future__ import annotations

import asyncio

import pytest

from mote.orchestration.background_tasks import require_bg_complete

from .conftest import gated, wait_started


@pytest.fixture(autouse=True)
def patch_reporter(monkeypatch):
    """Replace ThoughtReporter so require_bg_complete never POSTs anywhere."""
    reports = []

    class FakeReporter:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def async_report(self, value, name="object"):
            reports.append((value, name))

    monkeypatch.setattr("mote.orchestration.background_tasks.decorators.ThoughtReporter", FakeReporter)
    return reports


class TestRequireBgComplete:
    @pytest.mark.asyncio
    async def test_no_pending_runs_immediately(self, pool):
        calls = []

        @require_bg_complete(lambda: pool)
        async def work(x):
            calls.append(x)
            return x * 2

        assert await work(21) == 42
        assert calls == [21]

    @pytest.mark.asyncio
    async def test_none_pool_runs_immediately(self):
        @require_bg_complete(lambda: None)
        async def work():
            return "ran"

        assert await work() == "ran"

    @pytest.mark.asyncio
    async def test_waits_for_pending_then_runs(self, pool, patch_reporter):
        started, release = asyncio.Event(), asyncio.Event()

        async def bg(started, release):
            started.set()
            await release.wait()
            return "bg-done"

        pool.submit(lambda: bg(started, release), "background-job", timeout=None)
        await wait_started(started)

        order = []

        @require_bg_complete(lambda: pool)
        async def gated_work():
            order.append("work")
            return "work-done"

        runner = asyncio.create_task(gated_work())
        await asyncio.sleep(0)
        # Still blocked: the background task hasn't been released yet.
        assert order == []
        assert not runner.done()

        release.set()
        assert await asyncio.wait_for(runner, timeout=1) == "work-done"
        assert order == ["work"]
        assert pool.has_pending() is False
        # The gate reported a "waiting_bg_tasks" status while blocked.
        assert any(v.get("type") == "waiting_bg_tasks" for v, _ in patch_reporter)
        assert patch_reporter[0][0]["target_tool"] == "gated_work"
