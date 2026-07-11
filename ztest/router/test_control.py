#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for mote.router.control (router-control holds + target resolution)."""
from __future__ import annotations

import pytest
from mote.router.control import (
    DEFAULT_HOLD_TTL_SECONDS,
    RouterControlHold,
    RouterControlHoldStore,
    RouterControlValidationError,
    build_control_targets,
    resolve_control_target,
)

from .conftest import make_card


@pytest.fixture
def cards_map():
    return {
        "cheap": make_card("cheap", model="deepseek-chat", tier=0, description="cheap one"),
        "strong": make_card("strong", model="claude-3-haiku", tier=3),
    }


class TestBuildControlTargets:
    def test_targets_derived_from_cards(self, cards_map):
        targets = build_control_targets(cards_map)
        ids = {t.target_id for t in targets}
        assert ids == {"model:cheap", "model:strong"}
        by_name = {t.name: t for t in targets}
        assert by_name["cheap"].tier == 0
        assert by_name["cheap"].description == "cheap one"
        assert by_name["strong"].description is None  # empty -> None


class TestResolveControlTarget:
    def test_resolve_by_target_id(self, cards_map):
        t = resolve_control_target(cards_map, "model:strong")
        assert t.name == "strong"

    def test_resolve_by_bare_name(self, cards_map):
        t = resolve_control_target(cards_map, "cheap")
        assert t.target_id == "model:cheap"

    def test_blank_raises(self, cards_map):
        with pytest.raises(RouterControlValidationError):
            resolve_control_target(cards_map, "  ")

    def test_unknown_raises(self, cards_map):
        with pytest.raises(RouterControlValidationError):
            resolve_control_target(cards_map, "model:nope")


class TestRouterControlHoldExpiry:
    def _hold(self, **kw):
        defaults = dict(
            name="strong",
            tier=3,
            target_id="model:strong",
            evidence="",
            started_at_monotonic=100.0,
            last_activity_at_monotonic=100.0,
        )
        defaults.update(kw)
        return RouterControlHold(**defaults)

    def test_not_expired_within_ttl(self):
        hold = self._hold()
        expired, reason = hold.is_expired(100.0 + DEFAULT_HOLD_TTL_SECONDS - 1)
        assert expired is False
        assert reason is None

    def test_expired_on_ttl(self):
        hold = self._hold()
        expired, reason = hold.is_expired(100.0 + DEFAULT_HOLD_TTL_SECONDS)
        assert expired is True
        assert reason == "ttl"

    def test_expired_on_negative_turns(self):
        hold = self._hold(turns_remaining=-1)
        expired, reason = hold.is_expired(100.0)
        assert expired is True
        assert reason == "turn_count"

    def test_uses_started_when_no_activity(self):
        hold = self._hold(last_activity_at_monotonic=None)
        expired, _ = hold.is_expired(100.0 + DEFAULT_HOLD_TTL_SECONDS)
        assert expired is True


class TestRouterControlHoldStore:
    def _target(self, cards_map):
        return resolve_control_target(cards_map, "strong")

    def test_set_and_get_valid(self, cards_map):
        store = RouterControlHoldStore()
        store.set_hold("sess", self._target(cards_map), now_monotonic=0.0)
        hold = store.get_valid("sess", now_monotonic=1.0)
        assert hold is not None
        assert hold.name == "strong"

    def test_get_missing_returns_none(self):
        assert RouterControlHoldStore().get_valid("none") is None

    def test_clear(self, cards_map):
        store = RouterControlHoldStore()
        store.set_hold("sess", self._target(cards_map), now_monotonic=0.0)
        cleared = store.clear("sess")
        assert cleared is not None
        assert store.get_valid("sess") is None

    def test_expired_evicted_on_get(self, cards_map):
        store = RouterControlHoldStore()
        store.set_hold("sess", self._target(cards_map), now_monotonic=0.0, ttl_seconds=10.0)
        assert store.get_valid("sess", now_monotonic=11.0) is None

    def test_decrement_consumes_turn_budget(self, cards_map):
        store = RouterControlHoldStore()
        store.set_hold("sess", self._target(cards_map), now_monotonic=0.0, turns_remaining=1)
        # first decrement consumes the single turn and evicts the hold
        hold = store.get_valid("sess", now_monotonic=1.0, decrement=True)
        assert hold is not None
        assert store.get_valid("sess", now_monotonic=2.0) is None

    def test_decrement_updates_activity(self, cards_map):
        store = RouterControlHoldStore()
        store.set_hold("sess", self._target(cards_map), now_monotonic=0.0)
        store.get_valid("sess", now_monotonic=5.0, decrement=True)
        hold = store.get_valid("sess", now_monotonic=6.0)
        assert hold.last_activity_at_monotonic == 5.0
