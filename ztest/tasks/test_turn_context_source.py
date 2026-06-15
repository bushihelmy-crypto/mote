#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for :mod:`metagpt.tasks.turn_context_source`.

``BackgroundTaskContextSource`` adapts the background pool into an
``EphemeralContextSource``: it peeks the pool lazily (None until a tool spawns
one), builds its ``TaskAttachmentGenerator`` once, and renders running/finishing
tasks as ``<task-attachment>`` blocks (or None when nothing is in flight).
"""
from __future__ import annotations

import asyncio

from metagpt.common.interface import EphemeralContextSource
from metagpt.common.schema import BgStatus, TaskMeta
from metagpt.tasks import BackgroundTaskContextSource


def run(coro):
    return asyncio.run(coro)


class FakePool:
    def __init__(self, metas):
        self._metas = metas

    def list_tasks(self):
        return list(self._metas)


class TestBackgroundTaskContextSource:
    def test_is_ephemeral_context_source(self):
        src = BackgroundTaskContextSource(lambda: None)
        assert isinstance(src, EphemeralContextSource)

    def test_priority_and_name(self):
        src = BackgroundTaskContextSource(lambda: None)
        assert src.name == "background_tasks" and src.priority == 30

    def test_no_pool_returns_none(self):
        src = BackgroundTaskContextSource(lambda: None)
        assert run(src.render()) is None

    def test_empty_pool_returns_none(self):
        src = BackgroundTaskContextSource(lambda: FakePool([]))
        assert run(src.render()) is None

    def test_running_task_renders_attachment(self):
        meta = TaskMeta(task_id="bg_1", command_name="job", status=BgStatus.RUNNING)
        src = BackgroundTaskContextSource(lambda: FakePool([meta]))
        out = run(src.render())
        assert out is not None
        assert "<task-attachment>" in out
        assert "<task-id>bg_1</task-id>" in out
        assert "<status>running</status>" in out

    def test_generator_built_once_and_reused(self):
        meta = TaskMeta(task_id="bg_1", command_name="job", status=BgStatus.RUNNING)
        pool = FakePool([meta])
        src = BackgroundTaskContextSource(lambda: pool)
        run(src.render())
        gen1 = src._generator
        run(src.render())
        gen2 = src._generator
        assert gen1 is gen2 and gen1 is not None

    def test_lazy_pool_appears_later(self):
        # Pool starts as None (no tool spawned yet), then becomes available.
        holder = {"pool": None}
        src = BackgroundTaskContextSource(lambda: holder["pool"])
        assert run(src.render()) is None
        assert src._generator is None  # not built while pool absent

        meta = TaskMeta(task_id="bg_2", command_name="later", status=BgStatus.RUNNING)
        holder["pool"] = FakePool([meta])
        out = run(src.render())
        assert out is not None and "bg_2" in out
        assert src._generator is not None
