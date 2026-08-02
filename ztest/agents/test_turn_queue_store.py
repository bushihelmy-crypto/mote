from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path

import pytest

from mote.contracts.clock import UNIX_UTC_CLOCK, AbsoluteInstant
from mote.orchestration.agents.turn_queue import (
    TurnAcceptanceRequest,
    TurnAdmissionDisposition,
    TurnPriority,
    TurnQueueIdentity,
)
from mote.orchestration.agents.turn_queue.store import DurableTurnQueueStore, TurnQueueStoreError


class _LeaseCoordinator:
    def assert_current(self, subject: str, fencing_token: int) -> None:
        return None

    def guard(self, subject: str, fencing_token: int):
        return nullcontext()

    def acquire(self, subject: str, owner_id: str, ttl_seconds: float):
        raise NotImplementedError

    def renew(self, lease, ttl_seconds: float):
        raise NotImplementedError

    def release(self, lease) -> None:
        raise NotImplementedError


def _request(number: int = 1, *, priority: TurnPriority = TurnPriority.NORMAL) -> TurnAcceptanceRequest:
    return TurnAcceptanceRequest(
        identity=TurnQueueIdentity(
            "queue-1",
            f"request-{number}",
            "root-1",
            "subtree-1",
            f"agent-{number}",
            (f"delivery-{number}",),
        ),
        config_generation=1,
        priority=priority,
        accepted_at=AbsoluteInstant(1, UNIX_UTC_CLOCK, 10),
        deadline=AbsoluteInstant(1, UNIX_UTC_CLOCK, 20),
        maximum_attempts=3,
    )


def _store(path: Path, *, capacity: int = 2) -> DurableTurnQueueStore:
    return DurableTurnQueueStore(path, queue_id="queue-1", capacity=capacity, lease_coordinator=_LeaseCoordinator())


def test_accept_is_durable_and_duplicate_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "turns.json"
    store = _store(path)
    accepted = store.accept(_request())
    duplicate = _store(path).accept(_request())
    assert accepted.disposition is TurnAdmissionDisposition.ACCEPTED
    assert duplicate.disposition is TurnAdmissionDisposition.DUPLICATE
    snapshot = _store(path).load()
    assert snapshot.revision == 1
    assert snapshot.next_enqueue_sequence == 2
    assert tuple(item.identity.request_id for item in snapshot.items) == ("request-1",)


def test_same_request_identity_with_changed_acceptance_is_conflict(tmp_path: Path) -> None:
    store = _store(tmp_path / "turns.json")
    store.accept(_request())
    receipt = store.accept(_request(priority=TurnPriority.URGENT))
    assert receipt.disposition is TurnAdmissionDisposition.CONFLICT
    assert store.load().revision == 1


def test_queue_full_does_not_write_accepted_fact_or_advance_revision(tmp_path: Path) -> None:
    store = _store(tmp_path / "turns.json", capacity=1)
    store.accept(_request(1))
    before = store.load()
    rejected = store.accept(_request(2))
    after = store.load()
    assert rejected.disposition is TurnAdmissionDisposition.REJECTED_CAPACITY
    assert after == before
    assert all(item.identity.request_id != "request-2" for item in after.items)


@pytest.mark.parametrize(
    "payload",
    [b"{torn", json.dumps({"schema": "mote.agent-turn-queue/v9"}).encode()],
)
def test_corrupt_or_unknown_state_fails_closed_without_reset(tmp_path: Path, payload: bytes) -> None:
    path = tmp_path / "turns.json"
    path.write_bytes(payload)
    with pytest.raises(TurnQueueStoreError):
        _store(path).accept(_request())
    assert path.read_bytes() == payload


def test_composed_capacity_must_match_durable_envelope(tmp_path: Path) -> None:
    path = tmp_path / "turns.json"
    _store(path, capacity=2).accept(_request())
    with pytest.raises(TurnQueueStoreError, match="capacity"):
        _store(path, capacity=3).load()


def test_concurrent_accept_never_exceeds_capacity(tmp_path: Path) -> None:
    path = tmp_path / "turns.json"

    def accept(number: int) -> TurnAdmissionDisposition:
        return _store(path, capacity=3).accept(_request(number)).disposition

    with ThreadPoolExecutor(max_workers=6) as executor:
        dispositions = tuple(executor.map(accept, range(1, 7)))
    assert dispositions.count(TurnAdmissionDisposition.ACCEPTED) == 3
    snapshot = _store(path, capacity=3).load()
    assert len(snapshot.items) == 3
    assert snapshot.revision == 3
    assert tuple(item.enqueue_sequence for item in snapshot.items) == (1, 2, 3)
