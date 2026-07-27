"""Tests for provider-neutral semantic routing."""

from __future__ import annotations

import pytest

from mote.contracts.models.routing import RoutingInput, RoutingSignals
from mote.runtime.models.failover.transforms import CanonicalRequestTransformer


def test_default_route_returns_gateway_binding(router):
    route = router.model_route()

    assert route.route_id == "default"
    assert route.gateway is router.gateway
    assert route.profile.endpoint_id == "default"


def test_task_route_uses_mapping_and_unconfigured_task_falls_back_to_default(router):
    router.map_task("summary", "strong")

    assert router.model_route_for_task("summary").route_id == "strong"
    assert router.model_route_for_task("unknown").route_id == "default"


def test_compression_route_never_receives_recursive_transformer(router):
    router.context_reducer = object()

    assert router.model_route_for_task("compression").request_transformer is None


def test_interactive_route_binds_call_local_transformer(router):
    router.context_reducer = object()

    assert isinstance(router.model_route().request_transformer, CanonicalRequestTransformer)


def test_route_carries_session_scoped_capabilities(router):
    sink = object()
    resolver = object()
    router._session_fact_sink = sink
    router._artifact_resolver = resolver

    route = router.model_route()

    assert route.session_fact_sink is sink
    assert route.artifact_resolver is resolver


@pytest.mark.asyncio
async def test_intelligent_routing_returns_model_route_and_decision(router):
    route, decision = await router.aroute_model(
        RoutingInput(
            decision_id="decision-1",
            session_id="session-1",
            task="interactive",
            signals=RoutingSignals(prompt_text="debug this", flags=frozenset({"debug"})),
        )
    )

    assert route.route_id == decision.selected_route_id
    assert decision.selected_route_id == "strong"
    assert route.routing_decision_id == decision.decision_id


@pytest.mark.asyncio
async def test_intelligent_routing_filters_non_gateway_candidates(router):
    route, decision = await router.aroute_model(
        RoutingInput(
            decision_id="decision-2",
            session_id="session-1",
            task="interactive",
            signals=RoutingSignals(prompt_text="hello"),
        )
    )

    assert route.route_id == "default"
    assert decision.selected_route_id == "default"


def test_router_never_constructs_or_caches_provider_clients(router):
    assert not hasattr(router, "_instances")
    assert not hasattr(router, "_routed_clients")
    assert not hasattr(router, "_gateway_clients")
