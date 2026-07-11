#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for mote.router.squilla (SquillaStrategy — heuristic-fallback path).

The trained ML bundle (LightGBM / ONNX / sklearn) is NOT vendored, so the engine
is deliberately pointed at a missing ``model_dir`` and ``engine.predict`` returns
``None``. This exercises the deterministic heuristic fallback that still flows
through opensquilla's authoritative ``apply_postprocess`` pipeline, plus the
model-free helpers: ``score_to_probs``, the route index/class converters,
``thinking_mode_to_level``, ``detect_complaint``, ``_normalize_decisions`` and
``RoutingHistoryStore``.
"""
from __future__ import annotations

import pytest
from mote.router.control import build_control_targets
from mote.router.schema import RoutingRequest
from mote.router.squilla import (
    RoutingHistoryStore,
    SquillaStrategy,
    _normalize_decisions,
    detect_complaint,
    route_class_for_index,
    route_index,
    score_to_probs,
    thinking_mode_to_level,
)


@pytest.fixture
def squilla(tmp_path):
    """A strategy whose ML engine is unavailable → heuristic fallback path."""
    return SquillaStrategy(model_dir=tmp_path / "no-bundle")


class TestScoreToProbs:
    def test_returns_four_class_normalized(self):
        probs = score_to_probs(6.0)
        assert len(probs) == 4
        assert pytest.approx(sum(probs), abs=1e-9) == 1.0
        assert all(p >= 0 for p in probs)

    def test_low_score_peaks_low(self):
        probs = score_to_probs(0.0)
        assert probs[0] == max(probs)

    def test_high_score_peaks_high(self):
        probs = score_to_probs(12.0)
        assert probs[3] == max(probs)

    def test_score_is_clamped_to_span(self):
        # beyond the span saturates at the top class — same as span itself.
        assert score_to_probs(999.0) == score_to_probs(12.0)


class TestRouteIndexHelpers:
    def test_round_trip(self):
        for idx, cls in enumerate(["R0", "R1", "R2", "R3"]):
            assert route_index(cls) == idx
            assert route_class_for_index(idx) == cls

    def test_unknown_class_defaults_to_one(self):
        assert route_index("nope") == 1

    def test_index_is_clamped(self):
        assert route_class_for_index(-5) == "R0"
        assert route_class_for_index(99) == "R3"


class TestThinkingModeToLevel:
    def test_none_is_none(self):
        assert thinking_mode_to_level(None) is None

    def test_t0_is_none(self):
        assert thinking_mode_to_level("T0") is None

    def test_mapping(self):
        assert thinking_mode_to_level("T1") == "low"
        assert thinking_mode_to_level("T2") == "medium"
        assert thinking_mode_to_level("T3") == "high"


class TestNormalizeDecisions:
    def test_deep_thinking_forbids_p0(self):
        assert _normalize_decisions("T2", "P0") == ("T2", "P1")
        assert _normalize_decisions("T3", "P0") == ("T3", "P1")

    def test_shallow_thinking_keeps_p0(self):
        assert _normalize_decisions("T1", "P0") == ("T1", "P0")

    def test_non_p0_unchanged(self):
        assert _normalize_decisions("T3", "P2") == ("T3", "P2")


class TestDetectComplaint:
    def test_matches_terms(self):
        terms = detect_complaint("这个答案完全不对，重写")
        assert "完全不对" in terms
        assert "重写" in terms

    def test_english_terms(self):
        assert "wrong" in detect_complaint("this is wrong, try again")

    def test_clean_message_empty(self):
        assert detect_complaint("please summarize this document") == []

    def test_long_message_skipped(self):
        long = "完全不对 " + ("x" * 200)
        assert detect_complaint(long, max_chars=160) == []


class TestRoutingHistoryStore:
    def test_append_and_retrieve_within_window(self):
        store = RoutingHistoryStore(max_entries=5)
        store.append("s", "R2", now=100.0)
        assert store.previous_within_window("s", window=600.0, now=120.0) == "R2"

    def test_outside_window_returns_none(self):
        store = RoutingHistoryStore()
        store.append("s", "R3", now=100.0)
        assert store.previous_within_window("s", window=10.0, now=200.0) is None

    def test_unknown_session_returns_none(self):
        store = RoutingHistoryStore()
        assert store.previous_within_window("missing", window=600.0, now=1.0) is None

    def test_bounded_by_max(self):
        store = RoutingHistoryStore(max_entries=2)
        for i, cls in enumerate(["R0", "R1", "R2"]):
            store.append("s", cls, now=float(i))
        # only the last 2 retained; most-recent within window is R2.
        assert store.previous_within_window("s", window=600.0, now=10.0) == "R2"

    def test_clear_one_session(self):
        store = RoutingHistoryStore()
        store.append("a", "R1", now=1.0)
        store.append("b", "R2", now=1.0)
        store.clear("a")
        assert store.previous_within_window("a", window=600.0, now=2.0) is None
        assert store.previous_within_window("b", window=600.0, now=2.0) == "R2"

    def test_clear_all(self):
        store = RoutingHistoryStore()
        store.append("a", "R1", now=1.0)
        store.clear()
        assert store.previous_within_window("a", window=600.0, now=2.0) is None


class TestSquillaSelectFallback:
    @pytest.mark.asyncio
    async def test_engine_unavailable(self, squilla):
        assert squilla.engine.available is False

    @pytest.mark.asyncio
    async def test_no_candidates_falls_back(self, squilla):
        d = await squilla.select({}, RoutingRequest(text="x"), default="mid")
        assert d.fallback is True
        assert d.name == "mid"
        assert d.source == "squilla"

    @pytest.mark.asyncio
    async def test_trivial_prompt_marks_fallback_path(self, squilla, cards):
        d = await squilla.select(cards, RoutingRequest(text="好的，谢谢"), default="mid")
        assert d.source == "squilla"
        assert d.extra["ml"] is False
        assert d.tier in ("R0", "R1")

    @pytest.mark.asyncio
    async def test_complex_prompt_escalates_tier(self, squilla, cards):
        prompt = "请重新设计整个系统架构，迁移生产数据库，这是不可逆的高风险操作，" "需要跨所有模块进行重构并评估安全影响。"
        d = await squilla.select(cards, RoutingRequest(text=prompt), default="mid")
        assert d.tier in ("R2", "R3")
        assert d.name in ("vision", "strong")

    @pytest.mark.asyncio
    async def test_high_risk_flag_floors_tier(self, squilla, cards):
        req = RoutingRequest(text="好的", flags={"high_risk"})
        d = await squilla.select(cards, req, default="mid")
        assert d.tier in ("R2", "R3")

    @pytest.mark.asyncio
    async def test_vision_required_restricts_pool(self, squilla, cards):
        req = RoutingRequest(text="describe this image", requires_vision=True)
        d = await squilla.select(cards, req, default="mid")
        # only the vision (gpt-4o) card is capable.
        assert d.name == "vision"

    @pytest.mark.asyncio
    async def test_vision_required_no_capable_card_falls_back(self, squilla):
        from .conftest import make_card

        no_vision = {"a": make_card("a", model="deepseek-chat", tier=0)}
        req = RoutingRequest(text="x", requires_pdf=True)
        d = await squilla.select(no_vision, req, default="a")
        assert d.fallback is True

    @pytest.mark.asyncio
    async def test_control_hold_takes_precedence(self, squilla, cards):
        targets = {t.name: t for t in build_control_targets(cards)}
        squilla.control_holds.set_hold("default", targets["strong"])
        req = RoutingRequest(text="好的，谢谢", session_key="default")
        d = await squilla.select(cards, req, default="mid")
        assert d.name == "strong"
        assert d.extra["hold"] is True

    @pytest.mark.asyncio
    async def test_history_recorded_after_select(self, squilla, cards):
        req = RoutingRequest(text="redesign the whole architecture everywhere", session_key="hist")
        await squilla.select(cards, req, default="mid")
        assert squilla.history.previous_within_window("hist", window=600.0) is not None
