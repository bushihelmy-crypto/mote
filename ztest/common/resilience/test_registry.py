#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for :class:`ResourceHealthRegistry` — lazy per-key breaker sharing."""
from __future__ import annotations

from mote.common.resilience import (
    BreakerConfig,
    BreakerState,
    ResourceHealthRegistry,
    get_health_registry,
    reset_health_registry,
)


class TestRegistry:
    def test_same_key_returns_same_breaker(self):
        reg = ResourceHealthRegistry()
        assert reg.breaker("a") is reg.breaker("a")

    def test_distinct_keys_isolated(self):
        reg = ResourceHealthRegistry(BreakerConfig(min_samples=2, error_rate_threshold=0.5))
        # Trip only key "a".
        for _ in range(2):
            assert reg.admit("a")
            reg.record("a", False)
        assert reg.breaker("a").state is BreakerState.OPEN
        assert reg.breaker("b").state is BreakerState.CLOSED
        assert reg.admit("b") is True

    def test_snapshot(self):
        reg = ResourceHealthRegistry(BreakerConfig(min_samples=1, error_rate_threshold=0.5))
        reg.admit("x")
        reg.record("x", False)  # min_samples=1 → trips immediately
        reg.admit("y")
        reg.record("y", True)
        snap = reg.snapshot()
        assert snap == {"x": "open", "y": "closed"}

    def test_config_applied_to_created_breakers(self):
        reg = ResourceHealthRegistry(BreakerConfig(min_samples=99))
        assert reg.breaker("k").config.min_samples == 99

    def test_transition_hook_forwarded(self):
        events = []
        reg = ResourceHealthRegistry(
            BreakerConfig(min_samples=1, error_rate_threshold=0.5),
            on_transition=lambda k, o, n, r: events.append((k, n)),
        )
        reg.admit("z")
        reg.record("z", False)
        assert events == [("z", BreakerState.OPEN)]

    def test_set_transition_hook_affects_future_only(self):
        reg = ResourceHealthRegistry(BreakerConfig(min_samples=1, error_rate_threshold=0.5))
        reg.breaker("old")  # created before hook set
        seen = []
        reg.set_transition_hook(lambda k, o, n, r: seen.append(k))
        reg.breaker("new")
        reg.admit("new")
        reg.record("new", False)
        assert seen == ["new"]


class TestSingleton:
    def test_singleton_shared(self):
        reset_health_registry()
        assert get_health_registry() is get_health_registry()

    def test_reset_clears(self):
        reset_health_registry()
        first = get_health_registry()
        reset_health_registry()
        assert get_health_registry() is not first
