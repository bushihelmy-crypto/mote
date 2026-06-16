#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for :mod:`metagpt.tasks.decorators`.

Covers the ``bg_tool`` marker + ``is_bg_tool`` predicate and the
``require_bg_complete`` gate (no-wait fast path, wait-until-empty path, and the
``None``-pool path). ``ThoughtReporter`` is patched so the gate never touches
the network.
"""
from __future__ import annotations

import asyncio

import pytest

from metagpt.executor.tasks import bg_tool, is_bg_tool, require_bg_complete

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

    monkeypatch.setattr("metagpt.executor.tasks.decorators.ThoughtReporter", FakeReporter)
    return reports


class TestBgToolMarker:
    def test_bg_tool_sets_marker(self):
        @bg_tool
        async def f():
            return 1

        assert is_bg_tool(f) is True
        assert f._bg_tool is True

    def test_is_bg_tool_false_for_plain(self):
        async def g():
            return 1

        assert is_bg_tool(g) is False
        assert is_bg_tool(object()) is False


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

        pool.submit(bg(started, release), "background-job", timeout=None)
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
