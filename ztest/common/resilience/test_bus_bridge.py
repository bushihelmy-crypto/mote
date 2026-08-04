#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the breaker transition telemetry hook.

Wiring a registry's transition hook to :func:`breaker_telemetry_hook` makes each
breaker mirror its state changes onto active telemetry as
:class:`BreakerStateChangeEvent` observations. No telemetry bound → the hook is a
silent no-op (a breaker tripping outside a runtime scope never raises).
"""

from __future__ import annotations

from mote.contracts.events.model import BreakerStateChangeEvent
from mote.runtime.events import bind_telemetry, breaker_telemetry_hook
from mote.runtime.resilience import BreakerConfig, ResourceHealthRegistry
from mote.ztest.telemetry import InlineTelemetry


class _Collector:
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


class TestTelemetryBridge:
    def test_emits_on_transition(self):
        collector = _Collector()
        telemetry = InlineTelemetry(collector)
        with bind_telemetry(telemetry):
            reg = ResourceHealthRegistry(
                BreakerConfig(min_samples=2, error_rate_threshold=0.5),
                on_transition=breaker_telemetry_hook,
            )
            _trip_registry(reg, "llm::gpt::0")
        assert len(collector.events) == 1
        ev = collector.events[0]
        assert isinstance(ev, BreakerStateChangeEvent)
        assert ev.key == "llm::gpt::0"
        assert ev.old_state == "closed"
        assert ev.new_state == "open"
        assert ev.reason

    def test_no_telemetry_is_silent_noop(self):
        reg = ResourceHealthRegistry(
            BreakerConfig(min_samples=2, error_rate_threshold=0.5),
            on_transition=breaker_telemetry_hook,
        )
        _trip_registry(reg, "orphan")  # should not raise
