#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the breaker→event-bus bridge (:func:`breaker_bus_hook`).

Wiring a registry's transition hook to :func:`breaker_bus_hook` makes each
breaker mirror its state changes onto the active bus as
:class:`BreakerStateChangeEvent` observations. No bus bound → the hook is a
silent no-op (a breaker tripping outside a runtime scope never raises).
"""
from __future__ import annotations

from mote.common.events import BreakerStateChangeEvent, EventBus, breaker_bus_hook, set_bus
from mote.common.interface.event_subscriber import ObservationSubscriber, SyncObserver
from mote.common.resilience import BreakerConfig, ResourceHealthRegistry


class _Collector(ObservationSubscriber, SyncObserver):
    """Observation sink capturing every event (sync + async paths)."""

    def __init__(self) -> None:
        self.events: list = []

    async def handle(self, event) -> None:  # pragma: no cover - sync path used
        self.events.append(event)

    def handle_sync(self, event) -> None:
        self.events.append(event)


def _trip_registry(reg: ResourceHealthRegistry, key: str) -> None:
    for _ in range(2):
        assert reg.admit(key)
        reg.record(key, False)


class TestBusBridge:
    def test_emits_on_transition(self):
        bus = EventBus()
        collector = _Collector()
        bus.subscribe(collector)
        with set_bus(bus):
            reg = ResourceHealthRegistry(
                BreakerConfig(min_samples=2, error_rate_threshold=0.5),
                on_transition=breaker_bus_hook,
            )
            _trip_registry(reg, "llm::gpt::0")
        assert len(collector.events) == 1
        ev = collector.events[0]
        assert isinstance(ev, BreakerStateChangeEvent)
        assert ev.key == "llm::gpt::0"
        assert ev.old_state == "closed"
        assert ev.new_state == "open"
        assert ev.reason

    def test_no_bus_is_silent_noop(self):
        # No set_bus → observe_event_sync warns + drops; must not raise.
        reg = ResourceHealthRegistry(
            BreakerConfig(min_samples=2, error_rate_threshold=0.5),
            on_transition=breaker_bus_hook,
        )
        _trip_registry(reg, "orphan")  # should not raise
