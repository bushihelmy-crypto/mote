import asyncio

import pytest

from mote.contracts.inference.identity import TrustedSchedulingClass
from mote.runtime.inference.capacity import InFlightCapacity
from mote.runtime.inference.fair_queue import FairAdmissionQueue, QueueDeadlineExceededError, QueueEntry, QueueFullError


def _scheduling(*, tenant_weight=1, project_weight=1, cost=1, priority=0):
    return TrustedSchedulingClass(
        tenant_weight=tenant_weight,
        project_weight=project_weight,
        cost_units=cost,
        priority=priority,
    )


def test_queue_is_hard_bounded_and_deadline_aware():
    async def scenario():
        now = 10.0
        queue = FairAdmissionQueue(capacity=1, clock=lambda: now)
        await queue.enqueue("one", tenant_id="a", project_id="p", scheduling=_scheduling(), deadline=20)
        with pytest.raises(QueueFullError):
            await queue.enqueue("two", tenant_id="b", project_id="p", scheduling=_scheduling(), deadline=20)
        with pytest.raises(QueueDeadlineExceededError):
            await FairAdmissionQueue(capacity=1, clock=lambda: now).enqueue(
                "expired", tenant_id="a", project_id="p", scheduling=_scheduling(), deadline=10
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "kwargs",
    (
        {"capacity": True},
        {"capacity": 0},
        {"capacity": 1, "base_quantum": True},
        {"capacity": 1, "aging_seconds": float("nan")},
        {"capacity": 1, "aging_seconds": float("inf")},
    ),
)
def test_queue_rejects_invalid_capacity_quantum_and_aging(kwargs):
    with pytest.raises(ValueError, match="capacity, quantum and aging"):
        FairAdmissionQueue(**kwargs)


def test_queue_entry_and_enqueue_reject_non_finite_time() -> None:
    with pytest.raises(ValueError, match="deadline must be finite"):
        QueueEntry(1, "tenant", "project", "payload", 1, 0, 1.0, float("nan"))

    async def scenario() -> None:
        queue = FairAdmissionQueue(capacity=1, clock=lambda: 1.0)
        with pytest.raises(ValueError, match="deadline must be finite"):
            await queue.enqueue(
                "payload",
                tenant_id="tenant",
                project_id="project",
                scheduling=_scheduling(),
                deadline=float("nan"),
            )
        invalid_clock = FairAdmissionQueue(capacity=1, clock=lambda: float("nan"))
        with pytest.raises(ValueError, match="clock must return a finite"):
            await invalid_clock.enqueue(
                "payload",
                tenant_id="tenant",
                project_id="project",
                scheduling=_scheduling(),
                deadline=2.0,
            )

    asyncio.run(scenario())


def test_hierarchical_drr_serves_both_tenants_and_preserves_fifo_per_project():
    async def scenario():
        queue = FairAdmissionQueue(capacity=10, base_quantum=1, clock=lambda: 0.0)
        for value in ("a1", "a2", "a3"):
            await queue.enqueue(value, tenant_id="a", project_id="p", scheduling=_scheduling(), deadline=100)
        await queue.enqueue("b1", tenant_id="b", project_id="p", scheduling=_scheduling(), deadline=100)
        values = [(await queue.dequeue()).payload for _ in range(4)]
        assert values[:2] == ["a1", "b1"]
        assert values[2:] == ["a2", "a3"]

    asyncio.run(scenario())


def test_cancelled_and_expired_entries_never_dispatch():
    async def scenario():
        now = 0.0
        queue = FairAdmissionQueue(capacity=3, clock=lambda: now)
        cancelled = await queue.enqueue("cancel", tenant_id="a", project_id="p", scheduling=_scheduling(), deadline=10)
        await queue.enqueue("expire", tenant_id="a", project_id="p", scheduling=_scheduling(), deadline=1)
        await queue.enqueue("live", tenant_id="a", project_id="p", scheduling=_scheduling(), deadline=10)
        assert await queue.cancel(cancelled.entry_id)
        now = 2.0
        assert (await queue.dequeue()).payload == "live"
        assert queue.size == 0

    asyncio.run(scenario())


def test_in_flight_capacity_has_exactly_once_release_and_deadline():
    async def scenario():
        capacity = InFlightCapacity(1)
        loop = asyncio.get_running_loop()
        permit = await capacity.acquire(deadline=loop.time() + 1)
        with pytest.raises(TimeoutError):
            await capacity.acquire(deadline=loop.time() + 0.01)
        await permit.release()
        assert capacity.in_flight == 0
        with pytest.raises(RuntimeError, match="already released"):
            await permit.release()

    asyncio.run(scenario())
