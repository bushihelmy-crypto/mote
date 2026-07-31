import multiprocessing
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from mote.contracts.session.lease import RunLeasePolicy
from mote.runtime.errors import OutputCommitFencedError, RunLeaseUnavailableError
from mote.runtime.session.run_lease import RunLeaseHandle, RunLeaseStore


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _acquire_without_release(path, ready):
    lease = RunLeaseStore(path).acquire("run-1", "crashed-worker", 0.15)
    ready.put((lease.fencing_token, lease.expires_at))


class FailingRenewCoordinator:
    def __init__(self, store):
        self.store = store

    def acquire(self, run_id, owner_id, ttl_seconds):
        return self.store.acquire(run_id, owner_id, ttl_seconds)

    def renew(self, lease, ttl_seconds):
        raise OSError("coordinator unavailable")

    def release(self, lease):
        self.store.release(lease)

    def assert_current(self, run_id, fencing_token):
        self.store.assert_current(run_id, fencing_token)

    def guard(self, run_id, fencing_token):
        return self.store.guard(run_id, fencing_token)


def test_takeover_increments_token_and_fences_stale_owner(tmp_path):
    from mote.contracts.ports.session.run_lease import RunLeaseCoordinator

    clock = Clock()
    store = RunLeaseStore(tmp_path / "leases.json", clock=clock)
    assert isinstance(store, RunLeaseCoordinator)
    first = store.acquire("run-1", "worker-a", 10)

    clock.now = 111
    second = store.acquire("run-1", "worker-b", 10)

    assert first.fencing_token == 1
    assert second.fencing_token == 2
    with pytest.raises(OutputCommitFencedError):
        store.assert_current(first.run_id, first.fencing_token)
    store.assert_current(second.run_id, second.fencing_token)


def test_live_owner_blocks_takeover_and_renew_preserves_token(tmp_path):
    clock = Clock()
    store = RunLeaseStore(tmp_path / "leases.json", clock=clock)
    lease = store.acquire("run-1", "worker-a", 10)

    with pytest.raises(RunLeaseUnavailableError):
        store.acquire("run-1", "worker-b", 10)
    renewed = store.renew(lease, 20)

    assert renewed.fencing_token == lease.fencing_token
    assert renewed.expires_at == 120


def test_release_ends_epoch_and_next_acquire_increments_token(tmp_path):
    clock = Clock()
    store = RunLeaseStore(tmp_path / "leases.json", clock=clock)
    first = store.acquire("run-1", "worker-a", 10)
    store.release(first)

    with pytest.raises(OutputCommitFencedError):
        store.assert_current(first.run_id, first.fencing_token)
    second = store.acquire("run-1", "worker-b", 10)
    assert second.fencing_token == first.fencing_token + 1


def test_concurrent_first_acquire_has_one_winner(tmp_path):
    path = tmp_path / "leases.json"

    def acquire(owner):
        try:
            return RunLeaseStore(path).acquire("run-1", owner, 60)
        except RunLeaseUnavailableError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(acquire, ("worker-a", "worker-b")))

    winners = [outcome for outcome in outcomes if outcome is not None]
    assert len(winners) == 1
    assert winners[0].fencing_token == 1


@pytest.mark.asyncio
async def test_handle_heartbeats_and_releases_its_epoch(tmp_path, monkeypatch):
    import asyncio

    events = []

    async def capture(event):
        events.append(event)

    monkeypatch.setattr("mote.runtime.session.run_lease.observe_event", capture)

    store = RunLeaseStore(tmp_path / "leases.json")
    handle = RunLeaseHandle(
        store,
        run_id="run-1",
        owner_id="worker-a",
        policy=RunLeasePolicy(ttl_seconds=0.06, renew_interval_seconds=0.02),
    )
    await handle.start()
    initial_expiry = handle.lease.expires_at

    await asyncio.sleep(0.04)

    assert handle.lease.expires_at > initial_expiry
    token = handle.fencing_token
    await handle.close()
    with pytest.raises(OutputCommitFencedError):
        store.assert_current("run-1", token)
    assert events[0].phase == "acquired"
    assert "renewed" in [event.phase for event in events]
    assert events[-1].phase == "released"


def test_process_crash_expires_then_takeover_fences_old_epoch(tmp_path):
    path = tmp_path / "leases.json"
    context = multiprocessing.get_context("fork")
    ready = context.Queue()
    worker = context.Process(target=_acquire_without_release, args=(path, ready))
    worker.start()
    stale_token, expires_at = ready.get(timeout=2)
    worker.join(timeout=2)
    assert worker.exitcode == 0

    while time.time() <= expires_at:
        time.sleep(0.01)
    current = RunLeaseStore(path).acquire("run-1", "replacement-worker", 10)

    assert current.fencing_token == stale_token + 1
    with pytest.raises(OutputCommitFencedError):
        RunLeaseStore(path).assert_current("run-1", stale_token)


@pytest.mark.asyncio
async def test_heartbeat_backend_failure_blocks_later_commit(tmp_path, monkeypatch):
    import asyncio

    from mote.runtime.errors import RunLeaseCoordinatorUnavailableError

    events = []

    async def capture(event):
        events.append(event)

    monkeypatch.setattr("mote.runtime.session.run_lease.observe_event", capture)
    coordinator = FailingRenewCoordinator(RunLeaseStore(tmp_path / "leases.json"))
    handle = RunLeaseHandle(
        coordinator,
        run_id="run-1",
        owner_id="worker-a",
        policy=RunLeasePolicy(ttl_seconds=0.03, renew_interval_seconds=0.01),
    )
    await handle.start()
    await asyncio.sleep(0.02)

    with pytest.raises(RunLeaseCoordinatorUnavailableError) as caught:
        handle.assert_current("run-1", handle.fencing_token)

    assert caught.value.retryable is True
    assert caught.value.code.value == "RUN_LEASE_COORDINATOR_UNAVAILABLE"
    assert any(event.phase == "lost" and event.reason == "coordinator_unavailable" for event in events)
    await handle.close()


def test_lease_policy_rejects_unsafe_heartbeat_window():
    with pytest.raises(ValueError, match="less than ttl_seconds"):
        RunLeasePolicy(ttl_seconds=10, renew_interval_seconds=10)


def test_corrupt_coordinator_state_fails_with_typed_retryable_error(tmp_path):
    from mote.runtime.errors import RunLeaseCoordinatorUnavailableError

    path = tmp_path / "leases.json"
    path.write_text("not-json", encoding="utf-8")

    with pytest.raises(RunLeaseCoordinatorUnavailableError) as caught:
        RunLeaseStore(path).acquire("run-1", "worker-a", 10)

    assert caught.value.retryable is True
    assert caught.value.code.value == "RUN_LEASE_COORDINATOR_UNAVAILABLE"
