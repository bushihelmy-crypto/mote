"""Focused guarantees for the OS-held, monotonic Cron scheduler lease."""

from __future__ import annotations

import json

import pytest

from mote.contracts.clock import UNIX_UTC_CLOCK, AbsoluteInstant, ClockIdentity, MonotonicMark
from mote.orchestration.automation.cron.lock import SchedulerFenceLost, SchedulerLock, SchedulerLockCorruptionError


class FakeClock:
    def __init__(self) -> None:
        self.epoch_nanoseconds = 1_000

    @property
    def durable_clock_identity(self) -> ClockIdentity:
        return UNIX_UTC_CLOCK

    def now(self) -> AbsoluteInstant:
        return AbsoluteInstant(1, UNIX_UTC_CLOCK, self.epoch_nanoseconds)

    def monotonic_mark(self) -> MonotonicMark:
        return MonotonicMark("cron-lock-test", 0)


def test_only_one_owner_acquires_even_with_same_session_identity(tmp_path) -> None:
    first = SchedulerLock("same", base_dir=str(tmp_path), clock_source=FakeClock())
    second = SchedulerLock("same", base_dir=str(tmp_path), clock_source=FakeClock())

    fence = first.acquire()

    assert fence is not None
    assert first.acquire() == fence
    assert second.acquire() is None
    assert second.is_held is False
    first.release()


def test_release_preserves_epoch_and_reacquire_advances_fence(tmp_path) -> None:
    first = SchedulerLock("owner-a", base_dir=str(tmp_path), clock_source=FakeClock())
    first_fence = first.acquire()
    assert first_fence is not None
    first.refresh()
    first.release()

    released = json.loads(first.path.read_text(encoding="utf-8"))
    assert released["status"] == "released"
    assert released["epoch"] == first_fence.epoch

    second = SchedulerLock("owner-b", base_dir=str(tmp_path), clock_source=FakeClock())
    second_fence = second.acquire()
    assert second_fence is not None
    assert second_fence.epoch == first_fence.epoch + 1
    assert second_fence.token != first_fence.token
    second.release()


def test_stale_owner_cannot_refresh_or_release(tmp_path) -> None:
    stale = SchedulerLock("owner-a", base_dir=str(tmp_path), clock_source=FakeClock())
    assert stale.acquire() is not None
    stale.release()

    current = SchedulerLock("owner-b", base_dir=str(tmp_path), clock_source=FakeClock())
    assert current.acquire() is not None

    with pytest.raises(SchedulerFenceLost):
        stale.refresh()
    with pytest.raises(SchedulerFenceLost):
        stale.release()
    assert current.is_held
    current.release()


def test_corrupt_record_fails_closed_and_is_not_replaced(tmp_path) -> None:
    lock = SchedulerLock("owner", base_dir=str(tmp_path), clock_source=FakeClock())
    lock.path.parent.mkdir(parents=True, exist_ok=True)
    lock.path.write_text("not json", encoding="utf-8")

    with pytest.raises(SchedulerLockCorruptionError):
        lock.acquire()

    assert lock.path.read_text(encoding="utf-8") == "not json"
    assert lock.is_held is False
