#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for FileWatchService (Phase 4) — watcher -> FileChanged hook glue.

Uses a fake HookRunner recording every ``fire`` call to verify a change becomes
a ``FileChanged`` event with the right payload, that a misbehaving hook does not
break the watch loop, and that the real HookManager matches on the ``path`` field.
"""

from __future__ import annotations

import pytest

from mote.contracts.events.file.observation import FileMutatedEvent
from mote.contracts.file.errors import ReadCursorError
from mote.contracts.file.identity import FileChangeKind
from mote.contracts.file.views import ContinueReadRequest, TextReadRequest
from mote.contracts.hook import FileChangedInvocation
from mote.runtime.hook.manager import HookManager
from mote.runtime.hook.subscriber import HookSubscriber
from mote.runtime.hook.types import HookOutcome
from mote.runtime.watching.service import FILE_CHANGED_EVENT, FileWatchService
from mote.ztest.fileops_factory import FileOperations
from mote.ztest.telemetry import InlineTelemetry


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

    async def fire_file_changed(self, payload: FileChangedInvocation | object) -> HookOutcome:
        """Implement the canonical typed file-change hook port."""
        self.calls.append(
            (
                FILE_CHANGED_EVENT,
                (
                    payload.__dict__
                    if hasattr(payload, "__dict__")
                    else {
                        name: getattr(payload, name)
                        for name in ("path", "change_type", "prior_version", "version", "attribution")
                    }
                ),
            )
        )
        if self._raise:
            raise RuntimeError("boom")
        return HookOutcome()


def _service(tmp_path, runner=None, *, bus=None, **kwargs):
    telemetry = bus or InlineTelemetry()
    if runner is not None:
        telemetry.handlers.append(HookSubscriber(runner))
    state = tmp_path.parent / f"{tmp_path.name}-watch-service-state"
    operations = FileOperations(
        session_id=tmp_path.name,
        journal_path=state / "rollout.jsonl",
        get_project_root=lambda: str(tmp_path),
        lock_root=state / "locks",
    )
    service = FileWatchService(
        [str(tmp_path)],
        file_changes=operations,
        telemetry=telemetry,
        **kwargs,
    )
    return service, telemetry, operations


@pytest.mark.asyncio
async def test_change_fires_file_changed_hook(tmp_path):
    runner = FakeHookRunner()
    svc, _, _ = _service(tmp_path, runner)
    svc.watcher.prime()
    (tmp_path / "new.txt").write_text("hi")

    await svc.watcher.poll()

    assert len(runner.calls) == 1
    event, payload = runner.calls[0]
    assert event == FILE_CHANGED_EVENT
    assert payload["path"] == str(tmp_path / "new.txt")
    assert payload["change_type"] is FileChangeKind.CREATED
    assert payload["version"].size == len("hi")
    assert payload["version"].digest


@pytest.mark.asyncio
async def test_multiple_changes_fire_per_path(tmp_path):
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("1")
    runner = FakeHookRunner()
    svc, _, _ = _service(tmp_path, runner)
    svc.watcher.prime()
    a.write_text("1-changed")
    b.write_text("2")

    await svc.watcher.poll()

    assert len(runner.calls) == 2
    types = {p["change_type"] for _, p in runner.calls}
    assert types == {FileChangeKind.CREATED, FileChangeKind.MODIFIED}


@pytest.mark.asyncio
async def test_hook_failure_does_not_break_loop(tmp_path):
    runner = FakeHookRunner(raise_on_fire=True)
    svc, _, _ = _service(tmp_path, runner)
    svc.watcher.prime()
    (tmp_path / "x.txt").write_text("x")

    # Should not raise despite the hook raising.
    events = await svc.watcher.poll()
    assert len(events) == 1
    assert len(runner.calls) == 1


@pytest.mark.asyncio
async def test_ignore_forwarded_to_watcher(tmp_path):
    runner = FakeHookRunner()
    svc, _, _ = _service(tmp_path, runner, ignore=["*.pyc"])
    svc.watcher.prime()
    (tmp_path / "keep.txt").write_text("k")
    (tmp_path / "skip.pyc").write_text("s")

    await svc.watcher.poll()

    paths = {p["path"] for _, p in runner.calls}
    assert str(tmp_path / "keep.txt") in paths
    assert str(tmp_path / "skip.pyc") not in paths


@pytest.mark.asyncio
async def test_real_hookmanager_matches_on_path(tmp_path):
    # A FileChanged hook with a matcher selecting only *.py files.
    fired: list[str] = []

    def on_py_change(hook_input: FileChangedInvocation) -> None:
        fired.append(hook_input.payload.path)

    mgr = HookManager(session_id="s")
    mgr.register("FileChanged", on_py_change, matcher=r".*\.py$")

    svc, _, _ = _service(tmp_path, mgr)
    svc.watcher.prime()
    (tmp_path / "mod.py").write_text("x = 1")
    (tmp_path / "data.txt").write_text("nope")

    await svc.watcher.poll()

    assert fired == [str(tmp_path / "mod.py")]


@pytest.mark.asyncio
async def test_service_lifecycle(tmp_path):
    runner = FakeHookRunner()
    svc, _, _ = _service(tmp_path, runner)

    async def scenario():
        await svc.start_async()
        assert svc.watcher.is_running() is True
        await svc.stop()
        assert svc.watcher.is_running() is False

    await scenario()


@pytest.mark.asyncio
async def test_start_async_primes_off_loop_and_suppresses_initial_burst(tmp_path):
    """``start_async`` builds the baseline off the loop, so the first poll is quiet.

    Same guarantee as the explicit ``prime`` path — an existing file is part
    of the baseline, not reported as a fresh ``created`` — but the initial walk is
    scanned cooperatively so a large tree never monopolizes the event loop.
    """
    (tmp_path / "existing.txt").write_text("x")
    runner = FakeHookRunner()
    svc, _, _ = _service(tmp_path, runner)

    async def scenario():
        await svc.start_async()
        assert svc.watcher.is_running() is True
        events = await svc.watcher.poll()  # baseline already primed => no burst
        await svc.stop()
        return events

    assert await scenario() == []
    assert runner.calls == []


@pytest.mark.asyncio
async def test_file_mutated_event_does_not_claim_external_write(tmp_path):
    runner = FakeHookRunner()
    bus = InlineTelemetry()
    svc, _, _ = _service(tmp_path, runner, bus=bus)
    assert svc not in bus.handlers

    target = tmp_path / "f.txt"
    target.write_text("v1")
    svc.watcher.prime()

    async def scenario():
        # Simulate a tool writing the file then the bus emitting the event.
        target.write_text("v2-by-agent")
        await bus.emit(FileMutatedEvent(path=str(target), tool="Write"))
        return await svc.watcher.poll()

    events = await scenario()
    assert len(events) == 1
    assert len(runner.calls) == 1


@pytest.mark.asyncio
async def test_stop_does_not_mutate_bus_subscribers(tmp_path):
    runner = FakeHookRunner()
    bus = InlineTelemetry()
    svc, _, _ = _service(tmp_path, runner, bus=bus)
    assert svc not in bus.handlers
    await svc.stop()
    assert svc not in bus.handlers


@pytest.mark.asyncio
async def test_no_extra_handler_still_works(tmp_path):
    runner = FakeHookRunner()
    svc, _, _ = _service(tmp_path, runner)
    svc.watcher.prime()
    (tmp_path / "new.txt").write_text("hi")
    await svc.watcher.poll()
    assert len(runner.calls) == 1


@pytest.mark.asyncio
async def test_external_change_atomically_invalidates_path_and_cursor_epoch(tmp_path):
    target = tmp_path / "target.txt"
    sibling = tmp_path / "sibling.txt"
    target.write_text("target\nbody\n")
    sibling.write_text("sibling body")
    runner = FakeHookRunner()
    svc, _, operations = _service(tmp_path, runner)
    operations.capture(str(target))
    operations.capture(str(sibling))
    cursor = operations.read_view(
        str(target),
        TextReadRequest(limit=1),
    ).next_cursor
    assert cursor is not None
    prior_epoch = operations.cursor_registry.current_epoch
    svc.watcher.prime()

    target.write_text("external replacement")
    await svc.watcher.poll()

    assert operations.observed(str(target)) is None
    assert operations.observed(str(sibling)) is not None
    assert operations.cursor_registry.current_epoch == prior_epoch + 1
    with pytest.raises(ReadCursorError, match="stale timeline epoch"):
        operations.read_view(
            str(target),
            ContinueReadRequest(cursor=cursor),
        )
