from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from mote.contracts.errors.routing import RoutingUnavailableError
from mote.contracts.models.invocation import RequestRequirements
from mote.contracts.models.routing import (
    RouteAdmissionProfile,
    RouteCandidate,
    RouteCapabilities,
    RoutingDegradedReason,
    RoutingHints,
    RoutingHold,
    RoutingInput,
    RoutingProposal,
    RoutingSessionState,
    SeedFloor,
)
from mote.runtime.models.routing.catalog import RouteCatalogSnapshot
from mote.runtime.models.routing.policy import DeterministicRoutingPolicy
from mote.runtime.models.routing.service import RoutingService


class StateStore:
    def __init__(self, state=None):
        self.state = state or RoutingSessionState()

    async def read(self, _session_id):
        return self.state

    async def commit(self, _session_id, *, expected_generation, state):
        assert self.state.generation == expected_generation
        self.state = state


class FactSink:
    def __init__(self):
        self.events = []

    async def commit_fact(self, event):
        self.events.append(event)


class BlockingFactSink(FactSink):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def commit_fact(self, event):
        self.events.append(event)
        self.started.set()
        await self.release.wait()


class InvalidPolicy:
    policy_id = "invalid"
    policy_revision = "1"

    async def propose(self, routing_input, candidates, state):
        return RoutingProposal(
            selected_route_id="outside-scope",
            policy_id=self.policy_id,
            policy_revision=self.policy_revision,
            feature_schema_revision="1",
        )


class SlowPolicy:
    policy_id = "slow"
    policy_revision = "1"

    async def propose(self, routing_input, candidates, state):
        await asyncio.sleep(1)
        raise AssertionError("deadline failed to cancel policy")


class CancelledPolicy:
    policy_id = "cancelled"
    policy_revision = "1"

    async def propose(self, routing_input, candidates, state):
        raise asyncio.CancelledError


def candidate(
    route_id,
    rank,
    *,
    capabilities=None,
    variants=(),
    context_tokens=100_000,
):
    return RouteCandidate(
        route_id=route_id,
        quality_class=f"R{rank}",
        quality_rank=rank,
        context_tokens=context_tokens,
        capabilities=capabilities or RouteCapabilities(),
        admission_profiles=variants,
        allowed_regions=frozenset({"global"}),
    )


def routing_input(**updates):
    values = {
        "decision_id": "decision-1",
        "session_id": "session-1",
        "task": "interactive",
    }
    values.update(updates)
    return RoutingInput(**values)


def service(
    policy,
    candidates,
    *,
    state=None,
    sink=None,
    deadline_ms=50,
    utc_now=None,
):
    store = StateStore(state)
    fallback = DeterministicRoutingPolicy("standard")
    router = RoutingService(
        RouteCatalogSnapshot(
            revision="catalog-1",
            candidates=tuple(candidates),
            default_route_id="standard",
            class_routes=(),
        ),
        policy,
        fallback,
        store,
        deadline_ms=deadline_ms,
        session_fact_sink=sink,
        **({"utc_now": utc_now} if utc_now is not None else {}),
    )
    return router, store


@pytest.mark.asyncio
async def test_invalid_policy_output_falls_back_without_expanding_candidates():
    router, store = service(
        InvalidPolicy(),
        [candidate("standard", 1), candidate("strong", 3)],
    )
    decision = await router.decide(routing_input(caller_hints=RoutingHints(candidate_scope=("strong",))))
    assert decision.selected_route_id == "strong"
    assert decision.status == "fallback"
    assert decision.degraded_reason is RoutingDegradedReason.INVALID_PROPOSAL
    assert store.state.generation == 1


@pytest.mark.asyncio
async def test_explicit_empty_scope_fails_closed():
    router, _store = service(
        DeterministicRoutingPolicy("standard"),
        [candidate("standard", 1)],
    )
    with pytest.raises(RoutingUnavailableError):
        await router.decide(routing_input(caller_hints=RoutingHints(candidate_scope=())))


@pytest.mark.asyncio
async def test_pdf_is_not_satisfied_by_vision_and_hold_cannot_bypass_constraints():
    vision = RouteCapabilities(supports_vision=True)
    pdf = RouteCapabilities(supports_pdf=True)
    state = RoutingSessionState(control_hold=RoutingHold(target_route_id="vision", turns_remaining=2))
    router, store = service(
        DeterministicRoutingPolicy("vision"),
        [
            candidate("vision", 1, capabilities=vision),
            candidate("pdf", 2, capabilities=pdf),
        ],
        state=state,
    )
    decision = await router.decide(routing_input(requirements=RequestRequirements(needs_pdf=True)))
    assert decision.selected_route_id == "pdf"
    assert decision.degraded_reason is RoutingDegradedReason.HOLD_INADMISSIBLE
    assert store.state.control_hold is None


@pytest.mark.asyncio
async def test_expired_hold_and_seed_are_ignored_and_cleared_replayably():
    now = datetime(2026, 7, 25, tzinfo=timezone.utc)
    state = RoutingSessionState(
        control_hold=RoutingHold(
            target_route_id="strong",
            expires_at_utc=now - timedelta(seconds=1),
        ),
        seed_floor=SeedFloor(
            route_class="R3",
            expires_at_utc=now - timedelta(seconds=1),
        ),
    )
    router, store = service(
        DeterministicRoutingPolicy("standard"),
        [candidate("standard", 1), candidate("strong", 3)],
        state=state,
        utc_now=lambda: now,
    )

    decision = await router.decide(routing_input())

    assert decision.selected_route_id == "standard"
    assert decision.degraded_reason is RoutingDegradedReason.HOLD_EXPIRED
    assert store.state.control_hold is None
    assert store.state.seed_floor is None


@pytest.mark.asyncio
async def test_policy_deadline_uses_deterministic_local_fallback():
    router, _store = service(
        SlowPolicy(),
        [candidate("standard", 1)],
        deadline_ms=1,
    )
    decision = await router.decide(routing_input())
    assert decision.selected_route_id == "standard"
    assert decision.degraded_reason is RoutingDegradedReason.POLICY_TIMEOUT


@pytest.mark.asyncio
async def test_cancellation_does_not_commit_partial_state():
    router, store = service(CancelledPolicy(), [candidate("standard", 1)])
    with pytest.raises(asyncio.CancelledError):
        await router.decide(routing_input())
    assert store.state.generation == 0


@pytest.mark.asyncio
async def test_cancellation_during_commit_finishes_the_atomic_state_unit():
    sink = BlockingFactSink()
    router, store = service(
        DeterministicRoutingPolicy("standard"),
        [candidate("standard", 1)],
        sink=sink,
    )
    task = asyncio.create_task(router.decide(routing_input()))
    await sink.started.wait()
    task.cancel()
    sink.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(sink.events) == 1
    assert store.state.generation == 1
    assert sink.events[0].state == store.state.model_dump(mode="json")


@pytest.mark.asyncio
async def test_decision_fact_contains_complete_recoverable_next_state():
    sink = FactSink()
    router, store = service(
        DeterministicRoutingPolicy("standard"),
        [candidate("standard", 1)],
        sink=sink,
    )
    decision = await router.decide(routing_input(model_call_id="call-1"))
    assert decision.model_call_id == "call-1"
    assert sink.events[0].decision["decision_id"] == "decision-1"
    assert sink.events[0].state == store.state.model_dump(mode="json")


@pytest.mark.asyncio
async def test_route_variant_must_satisfy_all_constraints_coherently():
    route = candidate(
        "mixed",
        1,
        variants=(
            RouteAdmissionProfile(
                capabilities=RouteCapabilities(supports_tools=True),
                context_tokens=100_000,
            ),
            RouteAdmissionProfile(
                capabilities=RouteCapabilities(supports_vision=True),
                context_tokens=100_000,
            ),
        ),
    )
    router, _store = service(DeterministicRoutingPolicy("mixed"), [route])
    with pytest.raises(RoutingUnavailableError):
        await router.decide(routing_input(requirements=RequestRequirements(needs_tools=True, needs_vision=True)))
