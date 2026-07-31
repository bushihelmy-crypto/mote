#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the per-agent hard budget cap — ContextProvider.enforce_budget.

The gate is the run's single spend ceiling: it reads this agent's own
accrued spend (``context.cost_manager.total_cost``) against the schema's
``max_cost`` cap and rules whether the loop may think again. It surfaces a soft
``BudgetEvent`` once at 80% and a hard-stop one once at 100% (each latched), and
returns a stop verdict only at the hard cap. A non-positive cap disables it.
"""
from __future__ import annotations

import asyncio

import pytest

from mote.contracts.events.agent import BUDGET


@pytest.fixture
def captured_events(role, monkeypatch):
    """Record every event the provider emits onto Role telemetry."""
    events: list = []

    async def _record(event):
        events.append(event)

    monkeypatch.setattr(role.telemetry, "emit", _record)
    return events


def _spend(role, amount: float) -> None:
    role.context.cost_manager.total_cost = amount


class TestBudgetDisabled:
    def test_zero_cap_proceeds_and_is_silent(self, role, captured_events):
        role.role_schema.max_cost = 0.0
        _spend(role, 100.0)
        verdict = asyncio.run(role.context_provider.enforce_budget())
        assert verdict.stop is False
        assert captured_events == []

    def test_negative_cap_proceeds_and_is_silent(self, role, captured_events):
        role.role_schema.max_cost = -1.0
        _spend(role, 100.0)
        verdict = asyncio.run(role.context_provider.enforce_budget())
        assert verdict.stop is False
        assert captured_events == []


class TestBudgetThresholds:
    def test_under_warn_line_proceeds_silently(self, role, captured_events):
        role.role_schema.max_cost = 10.0
        _spend(role, 7.0)  # 70% < 80%
        verdict = asyncio.run(role.context_provider.enforce_budget())
        assert verdict.stop is False
        assert captured_events == []

    def test_soft_warning_emits_once(self, role, captured_events):
        role.role_schema.max_cost = 10.0
        _spend(role, 8.5)  # 85% ≥ 80%, < 100%
        cp = role.context_provider
        v1 = asyncio.run(cp.enforce_budget())
        v2 = asyncio.run(cp.enforce_budget())  # still over 80%
        assert v1.stop is False and v2.stop is False
        # latched: exactly one warning despite two checks
        assert len(captured_events) == 1
        ev = captured_events[0]
        assert ev.name == BUDGET
        assert ev.stopped is False
        assert ev.spend == 8.5 and ev.limit == 10.0

    def test_hard_cap_stops_with_message_and_emits_once(self, role, captured_events):
        role.role_schema.max_cost = 10.0
        _spend(role, 12.0)  # ≥ 100%
        cp = role.context_provider
        v1 = asyncio.run(cp.enforce_budget())
        v2 = asyncio.run(cp.enforce_budget())
        assert v1.stop is True and v2.stop is True
        assert v1.message  # non-empty final reply
        # latched: exactly one stop event despite two checks
        stop_events = [e for e in captured_events if e.stopped]
        assert len(stop_events) == 1
        assert stop_events[0].name == BUDGET

    def test_warn_then_stop_emits_both_once(self, role, captured_events):
        role.role_schema.max_cost = 10.0
        cp = role.context_provider
        _spend(role, 8.5)
        asyncio.run(cp.enforce_budget())  # warn
        _spend(role, 11.0)
        verdict = asyncio.run(cp.enforce_budget())  # stop
        assert verdict.stop is True
        assert len(captured_events) == 2
        assert captured_events[0].stopped is False
        assert captured_events[1].stopped is True
