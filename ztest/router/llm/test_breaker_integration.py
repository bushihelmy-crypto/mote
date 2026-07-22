#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Integration tests for the circuit breaker woven into the LLM call chokepoint.

``BaseLLM._run_with_recovery`` (the single LLM-call seam) admit-gates on, and
records to, a per-resource breaker when a :class:`ResourceHealthRegistry` is
wired onto ``_health_registry``. These tests drive that seam directly with a
fake ``send`` (no network) and a directly-constructed registry (no process
singleton, so no cross-test pollution), asserting:

- only RESOURCE-health failures (transient / credential) trip a breaker;
- an our-fault error (400/bad-request) never trips it;
- a success records health and never trips;
- once a resource's breaker is OPEN, the next call is SHED before the wire and
  fails over to a healthy provider via the existing FALLBACK recovery loop;
- with no registry wired the seam is fully inert.
"""
from __future__ import annotations

import asyncio

import pytest

from mote.common.config.config.llm_config import LLMConfig
from mote.common.exception import LLMAuthenticationError, LLMBadRequestError, LLMResourceUnavailableError
from mote.common.resilience import BreakerConfig, ResourceHealthRegistry
from mote.router.cost import CostTracker
from mote.router.llm.health import resource_key
from mote.router.llm.llm_response import LLMResponse
from mote.router.llm.openai_api import OpenAILLM


def run(coro):
    return asyncio.run(coro)


def _make_llm(model: str = "gpt-4o") -> OpenAILLM:
    cfg = LLMConfig(api_type="openai", base_url="https://api.openai.com/v1", model=model, api_key="sk-x", max_token=512)
    llm = OpenAILLM(cfg)
    llm.cost_manager = CostTracker()
    return llm


def _aggressive_registry() -> ResourceHealthRegistry:
    # Trip after 2 failures at >=50% error rate; hold OPEN long enough that a
    # follow-up call within a test is still shed (no time-based half-open).
    return ResourceHealthRegistry(BreakerConfig(min_samples=2, error_rate_threshold=0.5, open_seconds=100.0))


_MSGS = [{"role": "user", "content": "hi"}]


class TestRecording:
    def test_health_failure_trips_breaker(self):
        llm = _make_llm()
        reg = _aggressive_registry()
        llm._health_registry = reg
        key = resource_key(llm)

        async def _send(active, messages):
            raise LLMAuthenticationError("nope")

        # Two auth failures (ROTATE_CREDENTIAL → counts as health failure);
        # rotate degrades to False so each surfaces (re-raises) after recording.
        for _ in range(2):
            with pytest.raises(LLMAuthenticationError):
                run(llm._run_with_recovery(_send, _MSGS))
        assert reg.snapshot()[key] == "open"

    def test_bad_request_never_trips(self):
        llm = _make_llm()
        reg = _aggressive_registry()
        llm._health_registry = reg
        key = resource_key(llm)

        async def _send(active, messages):
            raise LLMBadRequestError("malformed")

        for _ in range(5):
            with pytest.raises(LLMBadRequestError):
                run(llm._run_with_recovery(_send, _MSGS))
        # ABORT-class error is our fault, not the provider's — no record, no trip.
        assert reg.snapshot().get(key, "closed") == "closed"

    def test_success_records_and_stays_closed(self):
        llm = _make_llm()
        reg = _aggressive_registry()
        llm._health_registry = reg
        key = resource_key(llm)

        async def _send(active, messages):
            return LLMResponse(content="ok")

        for _ in range(5):
            assert run(llm._run_with_recovery(_send, _MSGS)).content == "ok"
        assert reg.snapshot().get(key, "closed") == "closed"


class TestShedAndFailover:
    def test_open_breaker_sheds_call_before_wire(self):
        llm = _make_llm()
        reg = _aggressive_registry()
        llm._health_registry = reg
        wire_hits = {"n": 0}

        async def _send(active, messages):
            wire_hits["n"] += 1
            raise LLMAuthenticationError("nope")

        for _ in range(2):
            with pytest.raises(LLMAuthenticationError):
                run(llm._run_with_recovery(_send, _MSGS))
        assert wire_hits["n"] == 2  # both real attempts touched the wire

        # Breaker now OPEN + no fallback supplier → the shed error surfaces and
        # the wire is NOT touched a third time.
        with pytest.raises(LLMResourceUnavailableError):
            run(llm._run_with_recovery(_send, _MSGS))
        assert wire_hits["n"] == 2

    def test_open_breaker_fails_over_to_healthy_provider(self):
        primary = _make_llm("gpt-4o")
        healthy = _make_llm("gpt-4o-mini")  # distinct resource key
        healthy.cost_manager = CostTracker()
        reg = _aggressive_registry()
        primary._health_registry = reg
        healthy._health_registry = reg
        primary._fallback_supplier = lambda: healthy

        async def _send(active, messages):
            if active is primary:
                raise LLMAuthenticationError("nope")
            return LLMResponse(content="via-fallback")

        # Trip the primary's breaker.
        for _ in range(2):
            with pytest.raises(LLMAuthenticationError):
                run(primary._run_with_recovery(_send, _MSGS))
        assert reg.snapshot()[resource_key(primary)] == "open"

        # Next call: admit-gate sheds the primary (FALLBACK) → swaps to the
        # healthy provider whose own breaker admits → success.
        rsp = run(primary._run_with_recovery(_send, _MSGS))
        assert rsp.content == "via-fallback"


class TestInert:
    def test_no_registry_is_fully_inert(self):
        llm = _make_llm()
        assert llm._health_registry is None
        calls = {"n": 0}

        async def _send(active, messages):
            calls["n"] += 1
            raise LLMAuthenticationError("nope")

        # No registry → no gating, no recording; the error just surfaces.
        for _ in range(5):
            with pytest.raises(LLMAuthenticationError):
                run(llm._run_with_recovery(_send, _MSGS))
        assert calls["n"] == 5  # every call reached the wire (never shed)
