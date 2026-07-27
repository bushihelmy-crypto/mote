#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Shared provider-neutral fixtures for the router test suite."""
from __future__ import annotations

import pytest

from mote.contracts.models.routing import RouteCandidate, RouteCapabilities, RoutingSessionState
from mote.runtime.models.routing.catalog import RouteCatalogSnapshot
from mote.runtime.models.routing.policy import DeterministicRoutingPolicy
from mote.runtime.models.routing.service import RoutingService


class FakeLLM:
    """Minimal duck-typed stand-in for a BaseLLM used by router/strategy tests."""

    def __init__(self, name: str = "fake", reply: str = ""):
        self.name = name
        self.reply = reply
        self.aask_calls: list[str] = []
        self.aask_kwargs: list[dict] = []

    async def aask(self, prompt, stream=True, **kwargs):  # noqa: D401
        self.aask_calls.append(prompt)
        self.aask_kwargs.append(kwargs)
        return self.reply


@pytest.fixture
def router():
    """An LLMRouter over a canonical fake gateway and immutable route catalog."""
    from mote.runtime.models.gateway import LLMRouter
    from mote.ztest.model_fakes import FakeModelGateway

    gateway = FakeModelGateway(FakeLLM(name="gateway"))
    candidates = tuple(
        RouteCandidate(
            route_id=name,
            quality_class=f"R{rank}",
            quality_rank=rank,
            context_tokens=context_tokens,
            capabilities=RouteCapabilities(supports_vision=supports_vision),
            allowed_regions=frozenset({"global"}),
        )
        for name, rank, context_tokens, supports_vision in (
            ("cheap", 0, 8_000, False),
            ("mid", 1, 32_000, False),
            ("vision", 2, 128_000, True),
            ("strong", 3, 200_000, False),
        )
    ) + (
        RouteCandidate(
            route_id="default",
            quality_class="R1",
            quality_rank=1,
            context_tokens=200_000,
            allowed_regions=frozenset({"global"}),
        ),
    )

    class Store:
        async def read(self, _session_id):
            return self.state

        async def commit(self, _session_id, *, expected_generation, state):
            assert self.state.generation == expected_generation
            self.state = state

        state = RoutingSessionState()

    service = RoutingService(
        RouteCatalogSnapshot(
            revision="test-catalog",
            candidates=candidates,
            default_route_id="default",
            class_routes=(),
        ),
        DeterministicRoutingPolicy("default"),
        DeterministicRoutingPolicy("default"),
        Store(),
        deadline_ms=50,
    )
    return LLMRouter(
        gateway,
        routing_service=service,
    )
