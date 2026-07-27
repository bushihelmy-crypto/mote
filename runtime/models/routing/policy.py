"""Local deterministic routing policy and policy decorators."""

from __future__ import annotations

from mote.contracts.models.routing import (
    RouteCandidate,
    RoutingInput,
    RoutingProposal,
    RoutingSessionState,
    RoutingStateTransition,
)
from mote.contracts.ports import RoutingPolicy


class DeterministicRoutingPolicy:
    policy_id = "deterministic-rule"
    policy_revision = "1"

    def __init__(self, default_route_id: str) -> None:
        self._default_route_id = default_route_id

    async def propose(
        self,
        routing_input: RoutingInput,
        candidates: tuple[RouteCandidate, ...],
        state: RoutingSessionState,
    ) -> RoutingProposal:
        by_id = {candidate.route_id: candidate for candidate in candidates}
        reason = "default_route"
        if "high_risk" in routing_input.signals.flags or "debug" in routing_input.signals.flags:
            selected = max(candidates, key=lambda item: (item.quality_rank, item.route_id))
            reason = "risk_floor"
        elif routing_input.caller_hints.prefer_cheap:
            selected = min(candidates, key=lambda item: (item.quality_rank, item.route_id))
            reason = "prefer_cheap"
        elif "long_context" in routing_input.signals.flags:
            selected = max(
                candidates,
                key=lambda item: (
                    item.context_tokens,
                    item.quality_rank,
                    item.route_id,
                ),
            )
            reason = "long_context"
        elif self._default_route_id in by_id:
            selected = by_id[self._default_route_id]
        else:
            selected = min(candidates, key=lambda item: (item.quality_rank, item.route_id))
            reason = "deterministic_first"
        return RoutingProposal(
            selected_route_id=selected.route_id,
            policy_id=self.policy_id,
            policy_revision=self.policy_revision,
            feature_schema_revision="routing-input-v1",
            base_class=selected.quality_class,
            final_class=selected.quality_class,
            reason_codes=(reason,),
            explanation=reason,
            selection_kind="rule",
            state_transition=RoutingStateTransition(append_final_class=selected.quality_class),
        )


class ClassMappedRoutingPolicy:
    """Apply the activated R0-R3 mapping to a policy's class proposal."""

    def __init__(self, policy: RoutingPolicy, class_routes: dict[str, str]) -> None:
        self._policy = policy
        self._class_routes = dict(class_routes)
        self.policy_id = policy.policy_id
        self.policy_revision = policy.policy_revision

    async def propose(
        self,
        routing_input: RoutingInput,
        candidates: tuple[RouteCandidate, ...],
        state: RoutingSessionState,
    ) -> RoutingProposal:
        proposal = await self._policy.propose(routing_input, candidates, state)
        mapped = self._class_routes.get(proposal.final_class or "")
        return proposal.model_copy(update={"selected_route_id": mapped}) if mapped is not None else proposal


__all__ = [
    "ClassMappedRoutingPolicy",
    "DeterministicRoutingPolicy",
]
