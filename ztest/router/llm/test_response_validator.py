#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""RESPONSE-based FALLBACK (③): reject an unusable HTTP-200 → shed via FALLBACK.

Two layers under test:

- :func:`default_response_validator` — the conservative built-in (rejects ONLY a
  completely empty completion: no text AND no tool calls; a non-empty str/response
  and any unknown shape are accepted).
- ``BaseLLM._run_with_recovery`` wiring — a validator that returns a rejection
  reason after a *successful* ``send()`` raises a FALLBACK-classified
  :class:`LLMUnusableResponseError`, so the recovery loop sheds to another provider
  (a same-request retry would only reproduce the same output). Being a
  ``NonRetryableError`` it bypasses the inner transient-retry tier, and being
  FALLBACK-classified it does NOT count as a resource-health failure — so a content
  rejection never trips the breaker on an otherwise-healthy provider.
"""
from __future__ import annotations

import asyncio

import pytest

from mote.common.config.config.llm_config import LLMConfig
from mote.common.exception import LLMUnusableResponseError
from mote.common.resilience import BreakerConfig, ResourceHealthRegistry
from mote.router.cost import CostTracker
from mote.router.llm._validators import default_response_validator
from mote.router.llm.health import resource_key
from mote.router.llm.llm_response import LLMResponse, LLMToolCall
from mote.router.llm.openai_api import OpenAILLM


def run(coro):
    return asyncio.run(coro)


def _make_llm(model: str = "gpt-4o") -> OpenAILLM:
    cfg = LLMConfig(api_type="openai", base_url="https://api.openai.com/v1", model=model, api_key="sk-x", max_token=512)
    llm = OpenAILLM(cfg)
    llm.cost_manager = CostTracker()
    return llm


_MSGS = [{"role": "user", "content": "hi"}]


class TestDefaultResponseValidator:
    def test_empty_response_rejected(self):
        assert default_response_validator(LLMResponse(content="", tool_calls=[])) is not None

    def test_whitespace_only_response_rejected(self):
        assert default_response_validator(LLMResponse(content="   \n\t")) is not None

    def test_response_with_text_accepted(self):
        assert default_response_validator(LLMResponse(content="hello")) is None

    def test_response_with_tool_calls_accepted(self):
        rsp = LLMResponse(content="", tool_calls=[LLMToolCall(id="c1", name="Read", arguments={})])
        assert default_response_validator(rsp) is None

    def test_empty_str_rejected(self):
        assert default_response_validator("") is not None
        assert default_response_validator("   ") is not None

    def test_non_empty_str_accepted(self):
        assert default_response_validator("hi") is None

    def test_unknown_shape_accepted(self):
        # Don't second-guess an unfamiliar payload — accept, let downstream handle.
        assert default_response_validator({"weird": 1}) is None
        assert default_response_validator(None) is None


class TestValidatorInRecoveryLoop:
    def test_rejection_without_fallback_surfaces_error(self):
        llm = _make_llm()
        llm._response_validator = lambda result: "unusable"
        calls = {"n": 0}

        async def _send(active, messages):
            calls["n"] += 1
            return LLMResponse(content="anything")

        # No fallback supplier → the FALLBACK strategy degrades to a no-op and the
        # synthetic error surfaces.
        with pytest.raises(LLMUnusableResponseError):
            run(llm._run_with_recovery(_send, _MSGS))

    def test_rejection_fails_over_to_healthy_provider(self):
        primary = _make_llm("gpt-4o")
        healthy = _make_llm("gpt-4o-mini")
        # Reject the primary's (successful) response; accept the fallback's.
        primary._response_validator = lambda result: (
            None if getattr(result, "content", "") == "via-fallback" else "unusable"
        )
        healthy._response_validator = primary._response_validator
        primary._fallback_supplier = lambda: healthy

        async def _send(active, messages):
            if active is primary:
                return LLMResponse(content="empty-ish")
            return LLMResponse(content="via-fallback")

        rsp = run(primary._run_with_recovery(_send, _MSGS))
        assert rsp.content == "via-fallback"

    def test_accepted_response_passes_through(self):
        llm = _make_llm()
        llm._response_validator = lambda result: None  # never rejects

        async def _send(active, messages):
            return LLMResponse(content="ok")

        assert run(llm._run_with_recovery(_send, _MSGS)).content == "ok"

    def test_no_validator_is_inert(self):
        llm = _make_llm()
        assert llm._response_validator is None  # default slot unset on a bare provider

        async def _send(active, messages):
            return LLMResponse(content="")  # empty — but no validator to reject it

        assert run(llm._run_with_recovery(_send, _MSGS)).content == ""

    def test_rejection_does_not_trip_breaker(self):
        llm = _make_llm()
        reg = ResourceHealthRegistry(BreakerConfig(min_samples=2, error_rate_threshold=0.5, open_seconds=100.0))
        llm._health_registry = reg
        llm._response_validator = lambda result: "unusable"
        key = resource_key(llm)

        async def _send(active, messages):
            return LLMResponse(content="answered-but-unusable")

        # The wire call SUCCEEDS (resource recorded HEALTHY); the content rejection
        # happens OUTSIDE the health scope, so it sheds via FALLBACK without ever
        # impugning the provider's breaker.
        for _ in range(5):
            with pytest.raises(LLMUnusableResponseError):
                run(llm._run_with_recovery(_send, _MSGS))
        assert reg.snapshot().get(key, "closed") == "closed"


class TestRouterWiring:
    """The router stamps its ``response_validator`` onto every built/routed LLM."""

    def test_build_stamps_validator(self, router):
        llm = router.route(name="cheap")
        assert llm._response_validator is router.response_validator

    def test_route_with_llm_config_stamps_validator(self, router):
        cfg = LLMConfig(api_key="sk-test", model="gpt-4o")
        llm = router.route(llm_config=cfg)
        assert llm._response_validator is router.response_validator

    def test_default_validator_is_the_conservative_builtin(self, router):
        assert router.response_validator is default_response_validator

    def test_validator_can_be_overridden_and_restamped(self, router):
        custom = lambda result: None  # noqa: E731
        router.response_validator = custom
        router._instances.clear()  # force a rebuild with the new validator
        llm = router.route(name="mid")
        assert llm._response_validator is custom
