from __future__ import annotations

import pytest

from mote.contracts.resilience import BreakerConfig
from mote.runtime.models.failover.availability import AvailabilityBreaker
from mote.runtime.resilience import BreakerState


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _breaker(clock: FakeClock, **overrides) -> AvailabilityBreaker:
    values = {
        "min_samples": 1,
        "error_rate_threshold": 1.0,
        "open_seconds": 10.0,
        "probe_grace_seconds": 1.0,
    }
    values.update(overrides)
    return AvailabilityBreaker(BreakerConfig(**values), clock=clock)


def _permit(breaker: AvailabilityBreaker, deadline: float):
    permit = breaker.acquire(attempt_deadline=deadline)
    assert permit is not None
    return permit


def _trip(breaker: AvailabilityBreaker, deadline: float) -> None:
    _permit(breaker, deadline).fail()
    assert breaker.state is BreakerState.OPEN


def test_closed_failure_trips_and_fences_other_in_flight_results() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)
    failing = _permit(breaker, 110.0)
    late = _permit(breaker, 110.0)

    failing.fail()
    tripped_epoch = breaker.epoch
    late.succeed()

    assert breaker.state is BreakerState.OPEN
    assert breaker.epoch == tripped_epoch


def test_half_open_requires_success_quorum_before_closing() -> None:
    clock = FakeClock()
    breaker = _breaker(
        clock,
        half_open_max_probes=2,
        half_open_success_quorum=2,
    )
    _trip(breaker, 110.0)
    clock.advance(10.0)

    first = _permit(breaker, 120.0)
    second = _permit(breaker, 120.0)
    assert breaker.state is BreakerState.HALF_OPEN
    first.succeed()
    assert breaker.state is BreakerState.HALF_OPEN
    second.succeed()

    assert breaker.state is BreakerState.CLOSED


def test_probe_failure_reopens_and_fences_late_peer_success() -> None:
    clock = FakeClock()
    breaker = _breaker(
        clock,
        half_open_max_probes=2,
        half_open_success_quorum=2,
    )
    _trip(breaker, 110.0)
    clock.advance(10.0)
    failing = _permit(breaker, 120.0)
    late = _permit(breaker, 120.0)

    failing.fail()
    reopened_epoch = breaker.epoch
    late.succeed()

    assert breaker.state is BreakerState.OPEN
    assert breaker.epoch == reopened_epoch


def test_explicit_abandon_releases_probe_immediately() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)
    _trip(breaker, 110.0)
    clock.advance(10.0)

    _permit(breaker, 120.0).abandon()
    replacement = _permit(breaker, 120.0)
    replacement.succeed()

    assert breaker.state is BreakerState.CLOSED


def test_expired_probe_is_replaced_and_late_result_is_ignored() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)
    _trip(breaker, 110.0)
    clock.advance(10.0)
    expired = _permit(breaker, 115.0)
    assert expired.lease_deadline == 116.0

    clock.advance(6.0)
    replacement = _permit(breaker, 130.0)
    expired.succeed()
    assert breaker.state is BreakerState.HALF_OPEN
    replacement.succeed()

    assert breaker.state is BreakerState.CLOSED


def test_saturated_half_open_probe_pool_rejects_until_settled() -> None:
    clock = FakeClock()
    breaker = _breaker(clock)
    _trip(breaker, 110.0)
    clock.advance(10.0)

    probe = _permit(breaker, 120.0)
    assert breaker.acquire(attempt_deadline=120.0) is None
    probe.abandon()
    assert breaker.acquire(attempt_deadline=120.0) is not None


def test_disabled_breaker_is_inert_and_permits_still_settle_once() -> None:
    clock = FakeClock()
    breaker = AvailabilityBreaker(
        BreakerConfig(enabled=False, min_samples=1),
        clock=clock,
    )
    permit = _permit(breaker, 110.0)
    permit.fail()

    assert breaker.state is BreakerState.CLOSED
    assert breaker.epoch == 0
    with pytest.raises(RuntimeError, match="already settled"):
        permit.abandon()


def test_rejects_impossible_quorum() -> None:
    with pytest.raises(ValueError, match="cannot exceed"):
        AvailabilityBreaker(
            BreakerConfig(
                half_open_max_probes=1,
                half_open_success_quorum=2,
            )
        )
