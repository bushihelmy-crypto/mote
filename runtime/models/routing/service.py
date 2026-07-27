"""Guarded, deadline-bounded semantic routing state machine."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import datetime, timezone

from mote.contracts.errors.routing import RoutingProposalInvalidError, RoutingUnavailableError
from mote.contracts.events.types import RoutingDecisionEvent
from mote.contracts.models.invocation import ModelOperation, ResponseMode
from mote.contracts.models.routing import (
    RecentRoutingDecision,
    RouteCandidate,
    RoutingDecision,
    RoutingDegradedReason,
    RoutingInput,
    RoutingProposal,
    RoutingSessionState,
    RoutingStateTransition,
    SeedFloor,
)
from mote.contracts.ports import RoutingPolicy, RoutingStateStore, SessionFactSink
from mote.runtime.logging import log_class
from mote.runtime.models.routing.catalog import RouteCatalogSnapshot

_RECENT_DECISION_LIMIT = 5


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@log_class(
    level="DEBUG",
    exclude={
        "_admissible_candidates",
        "_apply_transition",
        "_commit_decision",
        "_decide",
        "_missing_constraints",
        "_missing_for_profile",
        "_primary_or_fallback",
        "_state_for_decision",
        "_validate_proposal",
    },
)
class RoutingService:
    def __init__(
        self,
        catalog: RouteCatalogSnapshot,
        policy: RoutingPolicy,
        fallback_policy: RoutingPolicy,
        state_store: RoutingStateStore,
        *,
        deadline_ms: float,
        session_fact_sink: SessionFactSink | None = None,
        utc_now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.catalog = catalog
        self.policy = policy
        self.fallback_policy = fallback_policy
        self.state_store = state_store
        self.deadline_seconds = deadline_ms / 1_000.0
        self.session_fact_sink = session_fact_sink
        self._utc_now = utc_now
        self._decision_lock = asyncio.Lock()

    async def decide(self, routing_input: RoutingInput) -> RoutingDecision:
        async with self._decision_lock:
            return await self._decide(routing_input)

    async def _decide(self, routing_input: RoutingInput) -> RoutingDecision:
        started = time.monotonic()
        state = await self.state_store.read(routing_input.session_id)
        policy_state, hold_expired, seed_expired = self._state_for_decision(state)
        candidates, missing = self._admissible_candidates(routing_input)
        if not candidates:
            raise RoutingUnavailableError(
                "no semantic route satisfies the request constraints",
                catalog_revision=self.catalog.revision,
                candidate_ids=[candidate.route_id for candidate in self.catalog.candidates],
                missing_by_candidate=missing,
            )

        proposal: RoutingProposal
        status = "selected"
        degraded: RoutingDegradedReason | None = (
            RoutingDegradedReason.HOLD_EXPIRED
            if hold_expired
            else (RoutingDegradedReason.SEED_EXPIRED if seed_expired else None)
        )

        hold = policy_state.control_hold
        if hold is not None and any(candidate.route_id == hold.target_route_id for candidate in candidates):
            held = next(candidate for candidate in candidates if candidate.route_id == hold.target_route_id)
            proposal = RoutingProposal(
                selected_route_id=held.route_id,
                policy_id="operator-hold",
                policy_revision="1",
                feature_schema_revision="routing-input-v1",
                final_class=held.quality_class,
                reason_codes=("operator_hold",),
                explanation=hold.evidence,
                selection_kind="hold",
                state_transition=RoutingStateTransition(
                    append_final_class=held.quality_class,
                    consume_hold=True,
                ),
            )
            status = "held"
        else:
            if hold is not None:
                degraded = RoutingDegradedReason.HOLD_INADMISSIBLE
            proposal, policy_degraded = await self._primary_or_fallback(
                routing_input,
                candidates,
                policy_state,
            )
            if policy_degraded is not None:
                degraded = policy_degraded
                status = "fallback"
            elif proposal.degraded_reason is not None:
                degraded = proposal.degraded_reason
                status = "fallback"

        self._validate_proposal(proposal, candidates)
        transition = proposal.state_transition
        if hold_expired:
            transition = transition.model_copy(update={"clear_hold": True})
        if seed_expired:
            transition = transition.model_copy(update={"clear_seed": True})
        if hold is not None and status != "held" and degraded is RoutingDegradedReason.HOLD_INADMISSIBLE:
            transition = transition.model_copy(update={"clear_hold": True})
        next_state = self._apply_transition(
            state,
            transition,
            routing_input,
            proposal,
        )
        latency_ms = max(0.0, (time.monotonic() - started) * 1_000.0)
        decision = RoutingDecision(
            decision_id=routing_input.decision_id,
            model_call_id=routing_input.model_call_id,
            selected_route_id=proposal.selected_route_id,
            policy_id=proposal.policy_id,
            policy_revision=proposal.policy_revision,
            feature_schema_revision=proposal.feature_schema_revision,
            catalog_revision=self.catalog.revision,
            state_generation=next_state.generation,
            status=status,
            degraded_reason=degraded,
            base_class=proposal.base_class,
            final_class=proposal.final_class,
            base_confidence=proposal.base_confidence,
            reason_codes=proposal.reason_codes,
            selection_kind=proposal.selection_kind,
            candidate_scores=proposal.scores,
            latency_ms=latency_ms,
        )

        commit_task = asyncio.create_task(
            self._commit_decision(
                routing_input.session_id,
                expected_generation=state.generation,
                decision=decision,
                state=next_state,
            )
        )
        try:
            await asyncio.shield(commit_task)
        except asyncio.CancelledError:
            await asyncio.shield(commit_task)
            raise
        return decision

    def _state_for_decision(self, state: RoutingSessionState) -> tuple[RoutingSessionState, bool, bool]:
        now = self._utc_now()
        hold_expired = bool(
            state.control_hold is not None
            and state.control_hold.expires_at_utc is not None
            and state.control_hold.expires_at_utc <= now
        )
        seed_expired = bool(
            state.seed_floor is not None
            and state.seed_floor.expires_at_utc is not None
            and state.seed_floor.expires_at_utc <= now
        )
        if not hold_expired and not seed_expired:
            return state, False, False
        return (
            state.model_copy(
                update={
                    "control_hold": None if hold_expired else state.control_hold,
                    "seed_floor": None if seed_expired else state.seed_floor,
                }
            ),
            hold_expired,
            seed_expired,
        )

    async def _commit_decision(
        self,
        session_id: str,
        *,
        expected_generation: int,
        decision: RoutingDecision,
        state: RoutingSessionState,
    ) -> None:
        """Finish the durable-fact/in-memory-state unit once commit begins."""

        if self.session_fact_sink is not None:
            await self.session_fact_sink.commit_fact(
                RoutingDecisionEvent(
                    decision=decision.model_dump(mode="json"),
                    state=state.model_dump(mode="json"),
                )
            )
        await self.state_store.commit(
            session_id,
            expected_generation=expected_generation,
            state=state,
        )

    async def seed_session(self, routing_input: RoutingInput) -> str:
        """Create a bounded raise-only seed without recording a fake decision."""

        async with self._decision_lock:
            state = await self.state_store.read(routing_input.session_id)
            policy_state, _hold_expired, _seed_expired = self._state_for_decision(state)
            candidates, missing = self._admissible_candidates(routing_input)
            if not candidates:
                raise RoutingUnavailableError(
                    "no semantic route can seed this session",
                    catalog_revision=self.catalog.revision,
                    missing_by_candidate=missing,
                )
            proposal, _degraded = await self._primary_or_fallback(
                routing_input,
                candidates,
                policy_state,
            )
            route_class = proposal.final_class or next(
                candidate.quality_class for candidate in candidates if candidate.route_id == proposal.selected_route_id
            )
            seeded = RoutingSessionState(
                generation=state.generation + 1,
                recent_decisions=policy_state.recent_decisions,
                seed_floor=SeedFloor(route_class=route_class, turns_remaining=5),
                control_hold=policy_state.control_hold,
            )
            await self.state_store.commit(
                routing_input.session_id,
                expected_generation=state.generation,
                state=seeded,
            )
            return route_class

    async def _primary_or_fallback(
        self,
        routing_input: RoutingInput,
        candidates: tuple[RouteCandidate, ...],
        state: RoutingSessionState,
    ) -> tuple[RoutingProposal, RoutingDegradedReason | None]:
        try:
            async with asyncio.timeout(self.deadline_seconds):
                proposal = await self.policy.propose(routing_input, candidates, state)
            self._validate_proposal(proposal, candidates)
            return proposal, None
        except TimeoutError:
            degraded = RoutingDegradedReason.POLICY_TIMEOUT
        except RoutingProposalInvalidError:
            degraded = RoutingDegradedReason.INVALID_PROPOSAL
        except asyncio.CancelledError:
            raise
        except Exception:
            degraded = RoutingDegradedReason.POLICY_ERROR

        fallback = await self.fallback_policy.propose(
            routing_input,
            candidates,
            state,
        )
        self._validate_proposal(fallback, candidates)
        return fallback, degraded

    def _admissible_candidates(
        self,
        routing_input: RoutingInput,
    ) -> tuple[tuple[RouteCandidate, ...], dict[str, list[str]]]:
        scope = routing_input.caller_hints.candidate_scope
        scoped_ids = set(scope) if scope is not None else None
        missing: dict[str, list[str]] = {}
        admitted: list[RouteCandidate] = []
        for candidate in self.catalog.candidates:
            reasons: list[str] = []
            if scoped_ids is not None and candidate.route_id not in scoped_ids:
                reasons.append("candidate_scope")
            if not candidate.enabled:
                reasons.append("disabled")
            reasons.extend(self._missing_constraints(candidate, routing_input))
            if reasons:
                missing[candidate.route_id] = reasons
            else:
                admitted.append(candidate)
        if scoped_ids is not None:
            for unknown in sorted(scoped_ids - {item.route_id for item in self.catalog.candidates}):
                missing[unknown] = ["unknown_route"]
        return tuple(admitted), missing

    @staticmethod
    def _missing_constraints(
        candidate: RouteCandidate,
        routing_input: RoutingInput,
    ) -> list[str]:
        if candidate.admission_profiles:
            alternatives = [
                RoutingService._missing_for_profile(
                    candidate,
                    routing_input,
                    profile.capabilities,
                    profile.context_tokens,
                    profile.governance_domain,
                    frozenset({profile.region}),
                )
                for profile in candidate.admission_profiles
            ]
            if any(not reasons for reasons in alternatives):
                return []
            return sorted(set.intersection(*(set(reasons) for reasons in alternatives))) or [
                "no_coherent_route_variant"
            ]
        return RoutingService._missing_for_profile(
            candidate,
            routing_input,
            candidate.capabilities,
            candidate.context_tokens,
            candidate.governance_domain,
            candidate.allowed_regions,
        )

    @staticmethod
    def _missing_for_profile(
        candidate: RouteCandidate,
        routing_input: RoutingInput,
        capabilities,
        context_tokens: int,
        governance_domain: str,
        allowed_regions: frozenset[str],
    ) -> list[str]:
        requirements = routing_input.requirements
        missing: list[str] = []
        if governance_domain != requirements.governance_domain:
            missing.append("governance_domain")
        if requirements.allowed_regions and not (allowed_regions & requirements.allowed_regions):
            missing.append("allowed_regions")
        if requirements.data_classification not in candidate.data_classifications:
            missing.append("data_classification")
        if (
            requirements.needs_tools or requirements.response_mode is ResponseMode.NATIVE_TOOLS
        ) and not capabilities.supports_tools:
            missing.append("tools")
        if (
            requirements.needs_native_schema or requirements.response_mode is ResponseMode.NATIVE_SCHEMA
        ) and not capabilities.supports_native_schema:
            missing.append("native_schema")
        if (
            requirements.needs_server_web_search or routing_input.operation is ModelOperation.WEB_SEARCH
        ) and not capabilities.supports_server_web_search:
            missing.append("server_web_search")
        if (
            requirements.needs_vision or routing_input.operation is ModelOperation.IMAGE_DESCRIPTION
        ) and not capabilities.supports_vision:
            missing.append("vision")
        if requirements.needs_pdf and not capabilities.supports_pdf:
            missing.append("pdf")
        if requirements.needs_native_tool_search and not capabilities.supports_native_tool_search:
            missing.append("native_tool_search")
        if context_tokens < requirements.min_context_tokens:
            missing.append("context_tokens")
        return missing

    @staticmethod
    def _validate_proposal(
        proposal: RoutingProposal,
        candidates: tuple[RouteCandidate, ...],
    ) -> None:
        candidate_ids = {candidate.route_id for candidate in candidates}
        if proposal.selected_route_id not in candidate_ids:
            raise RoutingProposalInvalidError(
                "routing policy selected a route outside the admissible set",
                selected_route_id=proposal.selected_route_id,
                admissible_route_ids=sorted(candidate_ids),
            )

    @staticmethod
    def _apply_transition(
        state: RoutingSessionState,
        transition: RoutingStateTransition,
        routing_input: RoutingInput,
        proposal: RoutingProposal,
    ) -> RoutingSessionState:
        recent = list(state.recent_decisions)
        recent.append(
            RecentRoutingDecision(
                decision_id=routing_input.decision_id,
                selected_route_id=proposal.selected_route_id,
                final_class=transition.append_final_class or proposal.final_class,
                turn_id=routing_input.turn_id,
            )
        )
        hold = state.control_hold
        if transition.clear_hold:
            hold = None
        elif transition.consume_hold and hold is not None and hold.turns_remaining is not None:
            remaining = hold.turns_remaining - 1
            hold = hold.model_copy(update={"turns_remaining": remaining}) if remaining > 0 else None
        seed = state.seed_floor
        if transition.clear_seed:
            seed = None
        elif transition.consume_seed and seed is not None and seed.turns_remaining is not None:
            remaining = seed.turns_remaining - 1
            seed = seed.model_copy(update={"turns_remaining": remaining}) if remaining > 0 else None
        return RoutingSessionState(
            generation=state.generation + 1,
            recent_decisions=tuple(recent[-_RECENT_DECISION_LIMIT:]),
            seed_floor=seed,
            control_hold=hold,
        )


__all__ = ["RoutingService"]
