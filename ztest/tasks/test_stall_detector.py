#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for :mod:`metagpt.tasks.stall_detector`.

Covers the interactive-prompt regex (``_matches_interactive_prompt``), the
watcher bookkeeping (``start_watching`` idempotence / ``stop_watching`` /
``stop_all``), and the monitor coroutine: a stalled task whose tail matches an
interactive prompt pushes a single ``stall_warning`` notification; a stalled
task with a benign tail pushes nothing; and a task that reaches a terminal
status makes the watcher exit on its own. Tiny intervals/thresholds keep the
test fast and deterministic; lightweight fakes drive the pool/store.
"""
from __future__ import annotations

import asyncio

import pytest

from metagpt.common.schema import BgStatus, MessageQueue, TaskMeta
from metagpt.tasks import StallDetector
from metagpt.tasks.stall_detector import _matches_interactive_prompt


class FakePool:
    def __init__(self, meta):
        self._meta = meta

    def get_task_info(self, task_id):
        return self._meta


class FakeStore:
    """Constant-size store (no output growth) returning a fixed tail."""

    def __init__(self, size: int, tail: bytes):
        self._size = size
        self._tail = tail

    def get_size(self, task_id):
        return self._size

    async def get_tail(self, task_id, max_bytes):
        return self._tail[-max_bytes:]


def make_detector(pool, store, buf):
    return StallDetector(
        pool,
        store,
        buf,
        stall_check_interval=0.005,
        stall_threshold=0.01,
        stall_tail_bytes=64,
    )


async def _wait_for(predicate, timeout=2.0):
    """Poll *predicate* until true or timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return False


class TestPromptRegex:
    @pytest.mark.parametrize(
        "text",
        [
            "Proceed? (y/n)",
            "Delete [y/n]",
            "Are you sure (yes/no)",
            "Press any key to continue",
            "Press Enter to continue",
            "Continue?",
            "Overwrite?",
            "Password:",
            "password :",
        ],
    )
    def test_matches(self, text):
        assert _matches_interactive_prompt(text) is True

    @pytest.mark.parametrize("text", ["building...", "Compiling module 3 of 10", "all good"])
    def test_no_match(self, text):
        assert _matches_interactive_prompt(text) is False


class TestWatcherBookkeeping:
    @pytest.mark.asyncio
    async def test_start_is_idempotent(self):
        meta = TaskMeta(task_id="bg_1", command_name="cmd", status=BgStatus.RUNNING)
        detector = make_detector(FakePool(meta), FakeStore(0, b""), MessageQueue())
        detector.start_watching("bg_1")
        first = detector._watchers["bg_1"]
        detector.start_watching("bg_1")  # no-op
        assert detector._watchers["bg_1"] is first
        detector.stop_all()

    @pytest.mark.asyncio
    async def test_stop_watching_and_stop_all(self):
        meta = TaskMeta(task_id="bg_1", command_name="cmd", status=BgStatus.RUNNING)
        detector = make_detector(FakePool(meta), FakeStore(0, b""), MessageQueue())
        detector.start_watching("bg_1")
        detector.start_watching("bg_2")
        detector.stop_watching("bg_1")
        assert "bg_1" not in detector._watchers
        detector.stop_all()
        assert detector._watchers == {}
        # stop_watching on an unknown id is a safe no-op.
        detector.stop_watching("ghost")


class TestMonitor:
    @pytest.mark.asyncio
    async def test_interactive_prompt_pushes_warning(self):
        buf = MessageQueue()
        meta = TaskMeta(task_id="bg_1", command_name="installer", status=BgStatus.RUNNING)
        detector = make_detector(FakePool(meta), FakeStore(50, b"Overwrite? (y/n)"), buf)
        detector.start_watching("bg_1")
        assert await _wait_for(lambda: not buf.empty())
        detector.stop_all()
        msgs = buf.pop_all()
        warnings = [m for m in msgs if getattr(m, "status", "") == "stall_warning"]
        assert warnings
        assert warnings[0].task_id == "bg_1"
        assert "stall_warning" in warnings[0].content

    @pytest.mark.asyncio
    async def test_benign_tail_no_warning(self):
        buf = MessageQueue()
        meta = TaskMeta(task_id="bg_1", command_name="build", status=BgStatus.RUNNING)
        detector = make_detector(FakePool(meta), FakeStore(50, b"compiling..."), buf)
        detector.start_watching("bg_1")
        # Give the monitor several cycles past the stall threshold.
        await asyncio.sleep(0.08)
        detector.stop_all()
        assert buf.empty()

    @pytest.mark.asyncio
    async def test_terminal_status_exits_watcher(self):
        buf = MessageQueue()
        meta = TaskMeta(task_id="bg_1", command_name="done-job", status=BgStatus.SUCCESS)
        detector = make_detector(FakePool(meta), FakeStore(50, b"Overwrite?"), buf)
        detector.start_watching("bg_1")
        # Watcher should observe the terminal status and remove itself.
        assert await _wait_for(lambda: "bg_1" not in detector._watchers)
        assert buf.empty()
