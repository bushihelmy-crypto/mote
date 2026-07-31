#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for OpenAILLM's family-matched unsupported-request-param drop.

``_UNSUPPORTED_REQUEST_PARAMS`` is keyed by model-name SUBSTRING (family), and
``_cons_kwargs`` drops the union of every matching key's params. This lets one
entry cover a whole family (``gpt-5`` → gpt-5.4 / gpt-5-mini) and drops
``temperature`` for Moonshot Kimi models, which 400 on any non-fixed value.

No network: only the pure ``_cons_kwargs`` / ``_unsupported_request_params``
builders are exercised.
"""
from __future__ import annotations

from mote.contracts.config.model.llm import LLMConfig
from mote.product.models.providers.openai_chat import OpenAILLM, _unsupported_request_params


def _make_llm(model: str, **overrides):
    cfg = LLMConfig(
        api_type="openai",
        base_url="https://api.example.com/v1",
        model=model,
        api_key="sk-test",  # pragma: allowlist secret
        max_token=2048,
        **overrides,
    )
    return OpenAILLM(cfg)


class TestUnsupportedRequestParams:
    def test_gpt5_family_substring_matches(self):
        # Exact "gpt-5" plus real family members reached by substring.
        for model in ("gpt-5", "gpt-5.4", "gpt-5-mini"):
            assert _unsupported_request_params(model) == frozenset({"max_tokens", "temperature"}), model

    def test_claude_opus_exact_entry(self):
        assert _unsupported_request_params("claude-opus-4-8") == frozenset({"temperature"})

    def test_kimi_family_drops_temperature(self):
        for model in ("kimi", "kimi-k2", "kimi-latest", "moonshot-v1-8k"):
            assert _unsupported_request_params(model) == frozenset({"temperature"}), model

    def test_unlisted_model_drops_nothing(self):
        assert _unsupported_request_params("deepseek-chat") == frozenset()
        assert _unsupported_request_params("gpt-4o") == frozenset()

    def test_none_and_empty(self):
        assert _unsupported_request_params(None) == frozenset()
        assert _unsupported_request_params("") == frozenset()


class TestConsKwargsDrops:
    def test_kimi_cons_kwargs_omits_temperature(self):
        llm = _make_llm("kimi-k2")
        kw = llm._cons_kwargs([{"role": "user", "content": "hi"}])
        assert "temperature" not in kw
        # max_tokens is NOT dropped for kimi (only temperature).
        assert "max_tokens" in kw

    def test_gpt5_cons_kwargs_omits_both(self):
        llm = _make_llm("gpt-5.4")
        kw = llm._cons_kwargs([{"role": "user", "content": "hi"}])
        assert "temperature" not in kw
        assert "max_tokens" not in kw

    def test_ordinary_model_keeps_params(self):
        llm = _make_llm("gpt-4o")
        kw = llm._cons_kwargs([{"role": "user", "content": "hi"}])
        assert "temperature" in kw
        assert "max_tokens" in kw


class TestReasoningEffort:
    def test_capable_model_gets_reasoning_effort(self):
        llm = _make_llm("gpt-5.4", reasoning_effort="high")  # gpt-5 → supports_thinking
        kw = llm._cons_kwargs([{"role": "user", "content": "hi"}])
        assert kw["reasoning_effort"] == "high"

    def test_incapable_model_never_gets_effort(self):
        # The latent-bug guard: an old model must not receive reasoning_effort.
        llm = _make_llm("gpt-4o", reasoning_effort="high")
        kw = llm._cons_kwargs([{"role": "user", "content": "hi"}])
        assert "reasoning_effort" not in kw

    def test_no_effort_no_param(self):
        llm = _make_llm("gpt-5.4")
        kw = llm._cons_kwargs([{"role": "user", "content": "hi"}])
        assert "reasoning_effort" not in kw
