#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for FileWatchService (Phase 4) — watcher -> FileChanged hook glue.

Uses a fake HookRunner recording every ``fire`` call to verify a change becomes
a ``FileChanged`` event with the right payload, that a misbehaving hook does not
break the watch loop, and that the real HookManager matches on the ``path`` field.
"""
from __future__ import annotations

import asyncio

from mote.common.events import EventBus, FileMutatedEvent
from mote.common.hook.manager import HookManager
from mote.common.hook.types import HookInput, HookOutcome
from mote.environment.watching.events import CREATED, MODIFIED
from mote.environment.watching.service import FILE_CHANGED_EVENT, FileWatchService


class FakeHookRunner:
    """Records every fire() call; optionally raises to test best-effort."""

    def __init__(self, *, raise_on_fire: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self._raise = raise_on_fire

    async def fire(self, event: str, payload: dict, *, permission_mode=None) -> HookOutcome:
        self.calls.append((event, payload))
        if self._raise:
            raise RuntimeError("boom")
        return HookOutcome()


def test_change_fires_file_changed_hook(tmp_path):
    runner = FakeHookRunner()
    svc = FileWatchService(runner, [str(tmp_path)])
    svc.watcher.prime()
    (tmp_path / "new.txt").write_text("hi")

    asyncio.run(svc.watcher.poll())

    assert len(runner.calls) == 1
    event, payload = runner.calls[0]
    assert event == FILE_CHANGED_EVENT
    assert payload["path"] == str(tmp_path / "new.txt")
    assert payload["change_type"] == CREATED
    assert payload["size"] == len("hi")


def test_multiple_changes_fire_per_path(tmp_path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("1")
    runner = FakeHookRunner()
    svc = FileWatchService(runner, [str(tmp_path)])
    svc.watcher.prime()
    a.write_text("1-changed")
    b.write_text("2")

    asyncio.run(svc.watcher.poll())

    assert len(runner.calls) == 2
    types = {p["change_type"] for _, p in runner.calls}
    assert types == {CREATED, MODIFIED}


def test_hook_failure_does_not_break_loop(tmp_path):
    runner = FakeHookRunner(raise_on_fire=True)
    svc = FileWatchService(runner, [str(tmp_path)])
    svc.watcher.prime()
    (tmp_path / "x.txt").write_text("x")

    # Should not raise despite the hook raising.
    events = asyncio.run(svc.watcher.poll())
    assert len(events) == 1
    assert len(runner.calls) == 1


def test_ignore_forwarded_to_watcher(tmp_path):
    runner = FakeHookRunner()
    svc = FileWatchService(runner, [str(tmp_path)], ignore=["*.pyc"])
    svc.watcher.prime()
    (tmp_path / "keep.txt").write_text("k")
    (tmp_path / "skip.pyc").write_text("s")

    asyncio.run(svc.watcher.poll())

    paths = {p["path"] for _, p in runner.calls}
    assert str(tmp_path / "keep.txt") in paths
    assert str(tmp_path / "skip.pyc") not in paths


def test_real_hookmanager_matches_on_path(tmp_path):
    # A FileChanged hook with a matcher selecting only *.py files.
    fired: list[str] = []

    def on_py_change(hook_input: HookInput) -> None:
        fired.append(hook_input.payload["path"])

    mgr = HookManager(session_id="s")
    mgr.register("FileChanged", on_py_change, matcher=r".*\.py$")

    svc = FileWatchService(mgr, [str(tmp_path)])
    svc.watcher.prime()
    (tmp_path / "mod.py").write_text("x = 1")
    (tmp_path / "data.txt").write_text("nope")

    asyncio.run(svc.watcher.poll())

    assert fired == [str(tmp_path / "mod.py")]


def test_service_lifecycle(tmp_path):
    runner = FakeHookRunner()
    svc = FileWatchService(runner, [str(tmp_path)])

    async def scenario():
        svc.start()
        assert svc.watcher.is_running() is True
        await svc.stop()
        assert svc.watcher.is_running() is False

    asyncio.run(scenario())


def test_start_async_primes_off_loop_and_suppresses_initial_burst(tmp_path):
    """``start_async`` builds the baseline off the loop, so the first poll is quiet.

    Same guarantee as the sync ``start``/``prime`` path — an existing file is part
    of the baseline, not reported as a fresh ``created`` — but the initial walk is
    pushed to an executor thread so a large tree never blocks the event loop.
    """
    (tmp_path / "existing.txt").write_text("x")
    runner = FakeHookRunner()
    svc = FileWatchService(runner, [str(tmp_path)])

    async def scenario():
        await svc.start_async()
        assert svc.watcher.is_running() is True
        events = await svc.watcher.poll()  # baseline already primed => no burst
        await svc.stop()
        return events

    assert asyncio.run(scenario()) == []
    assert runner.calls == []


def test_subscribes_to_bus_and_suppresses_self_write(tmp_path):
    """A FileMutatedEvent on the bus is recorded as a self-write and suppressed."""
    runner = FakeHookRunner()
    bus = EventBus()
    svc = FileWatchService(runner, [str(tmp_path)], bus=bus)
    assert svc in bus.subscribers  # subscribed itself on construction

    target = tmp_path / "f.txt"
    target.write_text("v1")
    svc.watcher.prime()

    async def scenario():
        # Simulate a tool writing the file then the bus emitting the event.
        target.write_text("v2-by-agent")
        await bus.emit(FileMutatedEvent(path=str(target), tool="Write"))
        return await svc.watcher.poll()

    events = asyncio.run(scenario())
    assert events == []  # our own write was suppressed
    assert runner.calls == []  # no FileChanged hook fired


def test_stop_unsubscribes_from_bus(tmp_path):
    runner = FakeHookRunner()
    bus = EventBus()
    svc = FileWatchService(runner, [str(tmp_path)], bus=bus)
    assert svc in bus.subscribers
    asyncio.run(svc.stop())
    assert svc not in bus.subscribers


def test_no_bus_still_works(tmp_path):
    """Without a bus the service behaves exactly as before (no subscription)."""
    runner = FakeHookRunner()
    svc = FileWatchService(runner, [str(tmp_path)])
    svc.watcher.prime()
    (tmp_path / "new.txt").write_text("hi")
    asyncio.run(svc.watcher.poll())
    assert len(runner.calls) == 1
