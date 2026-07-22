#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the domain-agnostic :class:`CircuitBreaker` state machine.

Pure-function tests driving a FAKE monotonic clock so cool-downs, window
eviction, and abandoned-probe leases are deterministic (no real sleeping).
Cover: the CLOSED→OPEN→HALF_OPEN→CLOSED cycle, sliding-window eviction +
min-samples gate, abandoned half-open probe reclaim, and the fail-open
``enabled=False`` inert mode.
"""
from __future__ import annotations

from mote.common.resilience import BreakerConfig, BreakerState, CircuitBreaker


class FakeClock:
    """A manually-advanced monotonic clock."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _fail(breaker: CircuitBreaker, n: int) -> None:
    for _ in range(n):
        assert breaker.admit()
        breaker.record(False)


class TestClosedState:
    def test_admits_when_healthy(self):
        b = CircuitBreaker(clock=FakeClock())
        assert b.state is BreakerState.CLOSED
        assert b.admit() is True

    def test_success_keeps_closed(self):
        b = CircuitBreaker(BreakerConfig(min_samples=3), clock=FakeClock())
        for _ in range(10):
            assert b.admit()
            b.record(True)
        assert b.state is BreakerState.CLOSED
        assert b.error_rate() == 0.0

    def test_below_min_samples_never_trips(self):
        # 100% failure but fewer than min_samples → still CLOSED.
        b = CircuitBreaker(BreakerConfig(min_samples=5), clock=FakeClock())
        _fail(b, 4)
        assert b.state is BreakerState.CLOSED

    def test_trips_at_threshold(self):
        b = CircuitBreaker(
            BreakerConfig(min_samples=5, error_rate_threshold=0.5),
            clock=FakeClock(),
        )
        _fail(b, 5)
        assert b.state is BreakerState.OPEN

    def test_mixed_below_threshold_stays_closed(self):
        b = CircuitBreaker(
            BreakerConfig(min_samples=4, error_rate_threshold=0.75),
            clock=FakeClock(),
        )
        # 2 of 4 fail = 0.5 < 0.75.
        b.admit()
        b.record(False)
        b.admit()
        b.record(True)
        b.admit()
        b.record(False)
        b.admit()
        b.record(True)
        assert b.state is BreakerState.CLOSED


class TestSlidingWindow:
    def test_old_failures_evicted(self):
        clock = FakeClock()
        b = CircuitBreaker(
            BreakerConfig(window_seconds=60, min_samples=5, error_rate_threshold=0.5),
            clock=clock,
        )
        _fail(b, 4)  # 4 failures, still under min_samples
        clock.advance(120)  # all age out of the 60s window
        # A single fresh failure alone is < min_samples → no trip.
        b.admit()
        b.record(False)
        assert b.state is BreakerState.CLOSED
        assert b.error_rate() == 1.0  # only the fresh one remains

    def test_error_rate_reflects_window(self):
        clock = FakeClock()
        b = CircuitBreaker(BreakerConfig(window_seconds=10), clock=clock)
        b.admit()
        b.record(False)
        b.admit()
        b.record(True)
        assert b.error_rate() == 0.5
        clock.advance(20)
        assert b.error_rate() == 0.0


class TestOpenState:
    def _tripped(self, clock):
        b = CircuitBreaker(
            BreakerConfig(min_samples=3, error_rate_threshold=0.5, open_seconds=20),
            clock=clock,
        )
        _fail(b, 3)
        assert b.state is BreakerState.OPEN
        return b

    def test_refuses_during_cooldown(self):
        clock = FakeClock()
        b = self._tripped(clock)
        assert b.admit() is False
        clock.advance(10)  # still within 20s cool-down
        assert b.admit() is False
        assert b.state is BreakerState.OPEN

    def test_half_open_probe_after_cooldown(self):
        clock = FakeClock()
        b = self._tripped(clock)
        clock.advance(20)
        assert b.admit() is True
        assert b.state is BreakerState.HALF_OPEN

    def test_record_ignored_while_open(self):
        clock = FakeClock()
        b = self._tripped(clock)
        # A call that raced past the just-tripped breaker records late.
        b.record(True)
        assert b.state is BreakerState.OPEN


class TestHalfOpen:
    def _half_open(self, clock, **cfg):
        b = CircuitBreaker(
            BreakerConfig(min_samples=3, error_rate_threshold=0.5, open_seconds=20, **cfg),
            clock=clock,
        )
        _fail(b, 3)
        clock.advance(20)
        assert b.admit() is True
        assert b.state is BreakerState.HALF_OPEN
        return b

    def test_probe_success_closes(self):
        clock = FakeClock()
        b = self._half_open(clock)
        b.record(True)
        assert b.state is BreakerState.CLOSED
        assert b.error_rate() == 0.0  # window cleared on close

    def test_probe_failure_reopens(self):
        clock = FakeClock()
        b = self._half_open(clock)
        b.record(False)
        assert b.state is BreakerState.OPEN

    def test_single_probe_slot_blocks_second(self):
        clock = FakeClock()
        b = self._half_open(clock)  # 1 probe claimed
        assert b.admit() is False  # slot taken, not yet abandoned

    def test_abandoned_probe_reclaimed(self):
        clock = FakeClock()
        b = self._half_open(clock)  # probe claimed, never recorded
        assert b.admit() is False
        clock.advance(20)  # lease (== open_seconds) elapses
        assert b.admit() is True  # reclaimed


class TestDisabled:
    def test_inert_always_admits_never_records(self):
        b = CircuitBreaker(BreakerConfig(enabled=False, min_samples=1), clock=FakeClock())
        for _ in range(100):
            assert b.admit() is True
            b.record(False)
        assert b.state is BreakerState.CLOSED


class TestTransitionHook:
    def test_hook_fires_on_transition(self):
        events = []
        clock = FakeClock()
        b = CircuitBreaker(
            BreakerConfig(min_samples=3, error_rate_threshold=0.5),
            key="res-x",
            clock=clock,
            on_transition=lambda k, o, n, r: events.append((k, o, n)),
        )
        _fail(b, 3)
        assert events == [("res-x", BreakerState.CLOSED, BreakerState.OPEN)]

    def test_hook_exception_swallowed(self):
        clock = FakeClock()

        def boom(*a):
            raise RuntimeError("observer down")

        b = CircuitBreaker(
            BreakerConfig(min_samples=3, error_rate_threshold=0.5),
            clock=clock,
            on_transition=boom,
        )
        _fail(b, 3)  # must not raise
        assert b.state is BreakerState.OPEN
