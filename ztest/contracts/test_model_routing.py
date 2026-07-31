from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from mote.contracts.model.routing import RouteCandidate, RoutingDecision, RoutingHold, RoutingInput, RoutingSessionState


def test_routing_contracts_are_frozen_forbid_extras_and_round_trip():
    routing_input = RoutingInput(
        decision_id="decision-1",
        session_id="session-1",
        task="interactive",
    )
    restored = RoutingInput.model_validate_json(routing_input.model_dump_json())
    assert restored == routing_input
    with pytest.raises(ValidationError):
        routing_input.session_id = "other"
    with pytest.raises(ValidationError):
        RouteCandidate(
            route_id="route",
            quality_class="R1",
            secret="must-not-enter-router",
        )


def test_decision_records_all_replay_revisions():
    decision = RoutingDecision(
        decision_id="decision-1",
        selected_route_id="interactive.standard",
        policy_id="rule",
        policy_revision="1",
        feature_schema_revision="input-v1",
        catalog_revision="catalog-v1",
        state_generation=1,
        status="selected",
        latency_ms=0.1,
    )
    assert decision.policy_revision == "1"
    assert decision.catalog_revision == "catalog-v1"
    assert RoutingSessionState().generation == 0


def test_recoverable_expiry_requires_utc_aware_time_and_round_trips():
    state = RoutingSessionState(
        control_hold=RoutingHold(
            target_route_id="strong",
            expires_at_utc=datetime(2026, 7, 25, tzinfo=timezone.utc),
        )
    )
    assert RoutingSessionState.model_validate_json(state.model_dump_json()) == state

    with pytest.raises(ValidationError):
        RoutingHold(
            target_route_id="strong",
            expires_at_utc=datetime(2026, 7, 25),
        )
