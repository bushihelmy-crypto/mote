#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for mote.router.schema (ModelCard / RoutingRequest / RoutingDecision)."""
from __future__ import annotations

from mote.router.schema import ModelCard, RoutingDecision, RoutingRequest

from .conftest import make_card


class TestModelCard:
    def test_supports_vision_true_for_gpt_substring(self):
        # 'gpt' is in MULTI_MODAL_MODELS, so any gpt-* model is vision-capable.
        assert make_card("c", model="gpt-3.5-turbo").supports_vision is True
        assert make_card("c", model="gpt-4o").supports_vision is True
        assert make_card("c", model="claude-3-opus").supports_vision is True
        assert make_card("c", model="gemini-1.5-pro").supports_vision is True

    def test_supports_vision_false_for_non_multimodal(self):
        assert make_card("c", model="deepseek-chat").supports_vision is False
        assert make_card("c", model="qwen-max").supports_vision is False
        assert make_card("c", model="claude-3-haiku").supports_vision is False

    def test_supports_vision_empty_model(self):
        card = ModelCard(name="x", llm_config=make_card("x", model="").llm_config)
        # model="" -> no substring matches
        assert card.supports_vision is False

    def test_defaults(self):
        card = make_card("c", model="deepseek-chat")
        assert card.tier == 1
        assert card.tags == set()
        assert card.description == ""
        assert card.context_window is None


class TestRoutingRequest:
    def test_token_estimate_explicit_wins(self):
        req = RoutingRequest(text="hello world", estimated_tokens=999, messages=[{"role": "user", "content": "x"}])
        assert req.token_estimate() == 999

    def test_token_estimate_from_text(self):
        req = RoutingRequest(text="hello world this is a longer string")
        assert req.token_estimate() > 0

    def test_token_estimate_empty(self):
        assert RoutingRequest().token_estimate() == 0

    def test_token_estimate_from_messages(self):
        req = RoutingRequest(messages=[{"role": "user", "content": "hello there friend"}])
        assert req.token_estimate() > 0

    def test_prompt_text_messages_precedence(self):
        req = RoutingRequest(
            text="fallback text",
            messages=[
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "second"},
            ],
        )
        assert req.prompt_text() == "first\nsecond"

    def test_prompt_text_falls_back_to_text(self):
        assert RoutingRequest(text="only text").prompt_text() == "only text"
        # messages present but all empty content -> fall through to text
        req = RoutingRequest(text="t", messages=[{"role": "user", "content": ""}])
        assert req.prompt_text() == "t"

    def test_defaults(self):
        req = RoutingRequest()
        assert req.flags == set()
        assert req.session_key == "default"
        assert req.requires_vision is False
        assert req.requires_pdf is False
        assert req.prefer_cheap is False


class TestRoutingDecision:
    def test_defaults(self):
        d = RoutingDecision(name="m")
        assert d.confidence == 1.0
        assert d.source == "rule"
        assert d.fallback is False
        assert d.reasons == []
        assert d.tier is None
        assert d.extra == {}

    def test_full(self):
        d = RoutingDecision(name="m", confidence=0.9, source="squilla", tier="R2", reasons=["x"], extra={"k": 1})
        assert d.source == "squilla"
        assert d.tier == "R2"
        assert d.reasons == ["x"]
        assert d.extra["k"] == 1
