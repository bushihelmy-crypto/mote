from __future__ import annotations

from pathlib import Path

from mote.contracts.clock import UNIX_UTC_CLOCK, AbsoluteInstant
from mote.orchestration.agents.turn_queue import (
    TurnAcceptanceRequest,
    TurnMutationDisposition,
    TurnPriority,
    TurnQueueIdentity,
    TurnQueueState,
)
from mote.orchestration.agents.turn_queue.limiter import AgentExecutionLimiter
from mote.orchestration.agents.turn_queue.scheduler import DurableTurnScheduler, TurnClaimDisposition
from mote.orchestration.agents.turn_queue.scheduling import TurnSchedulingConfig
from mote.orchestration.agents.turn_queue.store import DurableTurnQueueStore
from mote.runtime.control.leases import InMemoryLeaseCoordinator


def _instant(value: int) -> AbsoluteInstant:
    return AbsoluteInstant(1, UNIX_UTC_CLOCK, value)


def _request(number: int, *, maximum_attempts: int = 3) -> TurnAcceptanceRequest:
    return TurnAcceptanceRequest(
        TurnQueueIdentity(
            "queue-1",
            f"request-{number}",
            f"root-{number}",
            f"subtree-{number}",
            f"agent-{number}",
            (f"delivery-{number}",),
        ),
        1,
        TurnPriority.NORMAL,
        _instant(1),
        _instant(100),
        maximum_attempts,
        "digest-1",
    )


def _components(path: Path, *, now: list[float], limit: int = 1):
    leases = InMemoryLeaseCoordinator(clock=lambda: now[0])
    store = DurableTurnQueueStore(path, queue_id="queue-1", capacity=8, lease_coordinator=leases)
    limiter = AgentExecutionLimiter()
    limiter.initialize(limit)
    return leases, store, limiter, DurableTurnScheduler(store=store, limiter=limiter)


def _accept(store: DurableTurnQueueStore, request: TurnAcceptanceRequest) -> None:
    lease = store._lease_coordinator.acquire(f"turn-accept:{request.identity.request_id}", "acceptor", 30)
    prepared = store.prepare_acceptance(request, lease=lease)
    assert prepared.revision is not None
    store.commit_acceptance(
        request_id=request.identity.request_id, expected_item_revision=prepared.revision, lease=lease
    )


def test_claim_durably_binds_fence_process_and_execution_permit(tmp_path: Path) -> None:
    now = [1.0]
    leases, store, limiter, scheduler = _components(tmp_path / "turns.json", now=now)
    _accept(store, _request(1))
    lease = leases.acquire("turn-queue:queue-1", "scheduler-1", 30)
    attempt = scheduler.claim_next(
        config=TurnSchedulingConfig(1), now=_instant(10), lease=lease, process_instance_id="process-1"
    )
    assert attempt.disposition is TurnClaimDisposition.CLAIMED
    assert attempt.claim is not None
    assert limiter.active == 1
    durable = store.load().items[0]
    assert durable.state is TurnQueueState.CLAIMED
    assert durable.claim is not None
    assert durable.claim.scheduler_fencing_token == lease.fencing_token
    assert durable.claim.process_instance_id == "process-1"
    assert durable.claim.execution_permit_receipt == attempt.claim.execution_permit_receipt

    receipt = scheduler.settle(attempt.claim, succeeded=True, reason="completed", lease=lease)
    assert receipt.disposition is TurnMutationDisposition.APPLIED
    assert store.load().items[0].state is TurnQueueState.SUCCEEDED
    assert limiter.active == 0


def test_capacity_backpressure_does_not_claim_second_turn(tmp_path: Path) -> None:
    now = [1.0]
    leases, store, limiter, scheduler = _components(tmp_path / "turns.json", now=now)
    _accept(store, _request(1))
    _accept(store, _request(2))
    lease = leases.acquire("turn-queue:queue-1", "scheduler-1", 30)
    first = scheduler.claim_next(
        config=TurnSchedulingConfig(1), now=_instant(10), lease=lease, process_instance_id="process-1"
    )
    second = scheduler.claim_next(
        config=TurnSchedulingConfig(1), now=_instant(10), lease=lease, process_instance_id="process-1"
    )
    assert first.claim is not None
    assert second.disposition is TurnClaimDisposition.EXECUTION_BACKPRESSURE
    assert sum(item.state is TurnQueueState.CLAIMED for item in store.load().items) == 1
    assert limiter.active == 1


def test_stale_fence_cannot_claim_and_releases_only_its_local_permit(tmp_path: Path) -> None:
    now = [1.0]
    leases, store, limiter, scheduler = _components(tmp_path / "turns.json", now=now)
    _accept(store, _request(1))
    stale = leases.acquire("turn-queue:queue-1", "scheduler-1", 1)
    now[0] = 3.0
    current = leases.acquire("turn-queue:queue-1", "scheduler-2", 30)
    attempt = scheduler.claim_next(
        config=TurnSchedulingConfig(1), now=_instant(10), lease=stale, process_instance_id="process-1"
    )
    assert attempt.disposition is TurnClaimDisposition.STALE_FENCE
    assert limiter.active == 0
    assert store.load().items[0].state is TurnQueueState.ACCEPTED
    claimed = scheduler.claim_next(
        config=TurnSchedulingConfig(1), now=_instant(10), lease=current, process_instance_id="process-2"
    )
    assert claimed.disposition is TurnClaimDisposition.CLAIMED


def test_restart_scan_rediscovers_accepted_work_without_wake_signal(tmp_path: Path) -> None:
    now = [1.0]
    path = tmp_path / "turns.json"
    leases, store, _, _ = _components(path, now=now)
    _accept(store, _request(1))
    restarted_store = DurableTurnQueueStore(path, queue_id="queue-1", capacity=8, lease_coordinator=leases)
    restarted_limiter = AgentExecutionLimiter()
    restarted_limiter.initialize(1)
    scheduler = DurableTurnScheduler(store=restarted_store, limiter=restarted_limiter)
    lease = leases.acquire("turn-queue:queue-1", "scheduler-restarted", 30)
    attempt = scheduler.claim_next(
        config=TurnSchedulingConfig(2), now=_instant(10), lease=lease, process_instance_id="process-2"
    )
    assert attempt.disposition is TurnClaimDisposition.CLAIMED
    assert restarted_store.load().scheduling.config_generation == 2


def test_cancel_deadline_and_claim_compete_by_item_revision_cas(tmp_path: Path) -> None:
    now = [1.0]
    leases, store, limiter, scheduler = _components(tmp_path / "turns.json", now=now)
    _accept(store, _request(1))
    lease = leases.acquire("turn-queue:queue-1", "scheduler-1", 30)
    stale_view = store.load().items[0]
    cancelled = scheduler.cancel_unclaimed(stale_view, reason="root_cancelled", lease=lease)
    assert cancelled.disposition is TurnMutationDisposition.APPLIED
    assert scheduler.expire_unclaimed(stale_view, now=_instant(101), lease=lease).disposition in {
        TurnMutationDisposition.ALREADY_TERMINAL,
        TurnMutationDisposition.REVISION_CONFLICT,
    }
    assert (
        scheduler.claim_next(
            config=TurnSchedulingConfig(1), now=_instant(10), lease=lease, process_instance_id="process-1"
        ).disposition
        is TurnClaimDisposition.NO_ELIGIBLE
    )
    assert limiter.active == 0


def test_claimed_deadline_is_execution_owned_and_cannot_be_cancelled_as_queued(tmp_path: Path) -> None:
    now = [1.0]
    leases, store, limiter, scheduler = _components(tmp_path / "turns.json", now=now)
    _accept(store, _request(1))
    lease = leases.acquire("turn-queue:queue-1", "scheduler-1", 30)
    queued_view = store.load().items[0]
    claimed = scheduler.claim_next(
        config=TurnSchedulingConfig(1), now=_instant(10), lease=lease, process_instance_id="process-1"
    )
    assert claimed.claim is not None
    assert (
        scheduler.expire_unclaimed(queued_view, now=_instant(101), lease=lease).disposition
        is TurnMutationDisposition.REVISION_CONFLICT
    )
    assert (
        scheduler.cancel_unclaimed(queued_view, reason="late_cancel", lease=lease).disposition
        is TurnMutationDisposition.REVISION_CONFLICT
    )
    assert limiter.active == 1
    scheduler.settle(claimed.claim, succeeded=False, reason="execution_timeout", lease=lease)
    assert limiter.active == 0


def test_retry_persists_bounded_backoff_and_does_not_block_other_work(tmp_path: Path) -> None:
    now = [1.0]
    leases, store, limiter, scheduler = _components(tmp_path / "turns.json", now=now)
    _accept(store, _request(1))
    _accept(store, _request(2))
    lease = leases.acquire("turn-queue:queue-1", "scheduler-1", 30)
    first = scheduler.claim_next(
        config=TurnSchedulingConfig(1), now=_instant(10), lease=lease, process_instance_id="process-1"
    )
    assert first.claim is not None
    retried = scheduler.retry(first.claim, reason="transient", now=_instant(10), lease=lease)
    assert retried.disposition is TurnMutationDisposition.APPLIED
    retry_item = next(item for item in store.load().items if item.identity.request_id == "request-1")
    assert retry_item.state is TurnQueueState.ACCEPTED
    assert retry_item.next_eligible_at == _instant(1_000_000_010)
    assert limiter.active == 0
    second = scheduler.claim_next(
        config=TurnSchedulingConfig(1), now=_instant(10), lease=lease, process_instance_id="process-1"
    )
    assert second.claim is not None
    assert second.claim.item.identity.request_id == "request-2"


def test_durable_scan_expires_due_items_and_root_cancel_settles_each_acceptance(tmp_path: Path) -> None:
    now = [1.0]
    leases, store, _, scheduler = _components(tmp_path / "turns.json", now=now)
    _accept(store, _request(1))
    _accept(store, _request(2))
    lease = leases.acquire("turn-queue:queue-1", "scheduler-1", 30)
    expired = scheduler.reconcile_expired(now=_instant(101), lease=lease)
    assert len(expired) == 2
    assert all(receipt.state is TurnQueueState.EXPIRED for receipt in expired)

    path = tmp_path / "cancel.json"
    leases2, store2, _, scheduler2 = _components(path, now=now)
    _accept(store2, _request(1))
    _accept(store2, _request(2))
    lease2 = leases2.acquire("turn-queue:queue-1", "scheduler-2", 30)
    cancelled = scheduler2.cancel_root("root-1", reason="root_terminal", lease=lease2)
    assert len(cancelled) == 1
    assert cancelled[0].state is TurnQueueState.CANCELLED
    assert (
        next(item for item in store2.load().items if item.identity.root_id == "root-2").state is TurnQueueState.ACCEPTED
    )


def test_stale_owner_cannot_settle_or_retry_claimed_fact(tmp_path: Path) -> None:
    now = [1.0]
    leases, store, limiter, scheduler = _components(tmp_path / "turns.json", now=now)
    _accept(store, _request(1))
    stale = leases.acquire("turn-queue:queue-1", "scheduler-1", 1)
    claimed = scheduler.claim_next(
        config=TurnSchedulingConfig(1), now=_instant(10), lease=stale, process_instance_id="process-1"
    )
    assert claimed.claim is not None
    now[0] = 3.0
    leases.acquire("turn-queue:queue-1", "scheduler-2", 30)
    receipt = scheduler.retry(claimed.claim, reason="transient", now=_instant(11), lease=stale)
    assert receipt.disposition is TurnMutationDisposition.STALE_FENCE
    assert store.load().items[0].state is TurnQueueState.CLAIMED
    assert limiter.active == 0


def test_retry_exhaustion_is_a_terminal_typed_settlement(tmp_path: Path) -> None:
    now = [1.0]
    leases, store, limiter, scheduler = _components(tmp_path / "turns.json", now=now)
    _accept(store, _request(1, maximum_attempts=1))
    lease = leases.acquire("turn-queue:queue-1", "scheduler-1", 30)
    claimed = scheduler.claim_next(
        config=TurnSchedulingConfig(1), now=_instant(10), lease=lease, process_instance_id="process-1"
    )
    assert claimed.claim is not None
    receipt = scheduler.retry(claimed.claim, reason="poison", now=_instant(11), lease=lease)
    assert receipt.state is TurnQueueState.RETRY_EXHAUSTED
    item = store.load().items[0]
    assert item.state is TurnQueueState.RETRY_EXHAUSTED
    assert item.terminal_reason == "poison"
    assert item.next_eligible_at is None
    assert limiter.active == 0


def test_new_fenced_owner_can_terminally_reconcile_lost_claim_without_replay(tmp_path: Path) -> None:
    now = [1.0]
    leases, store, _, scheduler = _components(tmp_path / "turns.json", now=now)
    _accept(store, _request(1))
    old = leases.acquire("turn-queue:queue-1", "scheduler-1", 1)
    claimed = scheduler.claim_next(
        config=TurnSchedulingConfig(1), now=_instant(10), lease=old, process_instance_id="process-1"
    )
    assert claimed.claim is not None
    durable_claim = store.load().items[0]
    now[0] = 3.0
    current = leases.acquire("turn-queue:queue-1", "scheduler-2", 30)
    receipt = scheduler.settle_lost_claim(durable_claim, lease=current)
    assert receipt.disposition is TurnMutationDisposition.APPLIED
    assert receipt.state is TurnQueueState.FAILED
    settled = store.load().items[0]
    assert settled.terminal_reason == "execution_owner_lost"
    assert settled.claim is None
