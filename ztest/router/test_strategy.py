#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for mote.router.strategy (Rule / Complexity / LLMJudge strategies)."""
from __future__ import annotations

import pytest
from mote.router.schema import RoutingRequest
from mote.router.strategy import ComplexityStrategy, LLMJudgeStrategy, RuleBasedStrategy

from .conftest import FakeLLM

# cards fixture (cheap=0, mid=1, vision=2 [gpt-4o], strong=3) comes from conftest.


class TestRuleBasedStrategy:
    @pytest.mark.asyncio
    async def test_vision_picks_capable_card(self, cards):
        req = RoutingRequest(text="describe this", requires_vision=True)
        d = await RuleBasedStrategy().select(cards, req, default="mid")
        assert d.name == "vision"
        assert d.confidence == 0.9

    @pytest.mark.asyncio
    async def test_vision_no_capable_card_falls_back(self):
        from .conftest import make_card

        no_vision = {"a": make_card("a", model="deepseek-chat", tier=0)}
        req = RoutingRequest(text="x", requires_pdf=True)
        d = await RuleBasedStrategy().select(no_vision, req, default="a")
        assert d.fallback is True

    @pytest.mark.asyncio
    async def test_long_context_picks_fitting_high_tier(self, cards):
        req = RoutingRequest(text="x", estimated_tokens=100_000)
        d = await RuleBasedStrategy().select(cards, req, default="mid")
        # only vision(128k) and strong(200k) fit 100k; strong is higher tier.
        assert d.name == "strong"
        assert d.confidence == 0.8

    @pytest.mark.asyncio
    async def test_high_risk_flag_picks_strongest(self, cards):
        req = RoutingRequest(text="x", flags={"high_risk"})
        d = await RuleBasedStrategy().select(cards, req, default="mid")
        assert d.name == "strong"
        assert d.confidence == 0.85

    @pytest.mark.asyncio
    async def test_prefer_cheap_picks_cheapest(self, cards):
        req = RoutingRequest(text="x", prefer_cheap=True)
        d = await RuleBasedStrategy().select(cards, req, default="mid")
        assert d.name == "cheap"
        assert d.confidence == 0.8

    @pytest.mark.asyncio
    async def test_no_signal_returns_default(self, cards):
        req = RoutingRequest(text="just a normal short question")
        d = await RuleBasedStrategy().select(cards, req, default="mid")
        assert d.name == "mid"
        assert d.confidence == 0.5


class TestComplexityStrategy:
    @pytest.mark.asyncio
    async def test_simple_prompt_picks_low_tier(self, cards):
        req = RoutingRequest(text="show me the file")
        d = await ComplexityStrategy().select(cards, req, default="mid")
        # LOW band → cheapest tier card (cheap, tier 0)
        assert d.name == "cheap"
        assert d.tier == "LOW"
        assert d.source == "complexity"

    @pytest.mark.asyncio
    async def test_architecture_prompt_higher_tier(self, cards):
        prompt = (
            "Refactor and redesign the entire system architecture across all modules "
            "and migrate the production database. This is critical and irreversible."
        )
        req = RoutingRequest(text=prompt)
        d = await ComplexityStrategy().select(cards, req, default="mid")
        assert d.tier in ("MEDIUM", "HIGH")
        assert d.name in ("vision", "strong")

    @pytest.mark.asyncio
    async def test_prefer_cheap_forces_low(self, cards):
        req = RoutingRequest(text="redesign the whole architecture everywhere", prefer_cheap=True)
        d = await ComplexityStrategy().select(cards, req, default="mid")
        assert d.tier == "LOW"
        assert d.name == "cheap"

    @pytest.mark.asyncio
    async def test_high_risk_flag_forces_high(self, cards):
        req = RoutingRequest(text="show me the file", flags={"high_risk"})
        d = await ComplexityStrategy().select(cards, req, default="mid")
        assert d.tier == "HIGH"
        assert d.name == "strong"

    @pytest.mark.asyncio
    async def test_vision_required_respects_band(self, cards):
        req = RoutingRequest(text="show me the file", requires_vision=True)
        d = await ComplexityStrategy().select(cards, req, default="mid")
        # only vision card supports vision, so it must be picked.
        assert d.name == "vision"
        assert "requires vision/pdf" in d.reasons[0]

    @pytest.mark.asyncio
    async def test_no_candidates_falls_back(self):
        req = RoutingRequest(text="x")
        d = await ComplexityStrategy().select({}, req, default="mid")
        assert d.fallback is True
        assert d.name == "mid"


class TestLLMJudgeStrategy:
    @pytest.mark.asyncio
    async def test_exact_match(self, cards):
        llm = FakeLLM(reply="strong")
        d = await LLMJudgeStrategy(llm).select(cards, RoutingRequest(text="hard"), default="mid")
        assert d.name == "strong"
        assert d.confidence == 0.9
        assert d.source == "llm_judge"

    @pytest.mark.asyncio
    async def test_substring_match(self, cards):
        llm = FakeLLM(reply="I would choose the strong model here.")
        d = await LLMJudgeStrategy(llm).select(cards, RoutingRequest(text="hard"), default="mid")
        assert d.name == "strong"
        assert d.confidence == 0.7

    @pytest.mark.asyncio
    async def test_unmatched_reply_falls_back(self, cards):
        llm = FakeLLM(reply="none of these")
        d = await LLMJudgeStrategy(llm).select(cards, RoutingRequest(text="x"), default="mid")
        assert d.name == "mid"
        assert d.fallback is True

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back(self, cards):
        class BoomLLM:
            async def aask(self, prompt, stream=True, **kw):
                raise RuntimeError("provider down")

        d = await LLMJudgeStrategy(BoomLLM()).select(cards, RoutingRequest(text="x"), default="mid")
        assert d.fallback is True
        assert d.name == "mid"

    @pytest.mark.asyncio
    async def test_prompt_includes_candidate_names(self, cards):
        llm = FakeLLM(reply="mid")
        await LLMJudgeStrategy(llm).select(cards, RoutingRequest(text="x"), default="mid")
        prompt = llm.aask_calls[0]
        for name in cards:
            assert name in prompt
