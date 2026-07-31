"""Narrow extension ports for semantic routing policies and state."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from mote.contracts.model.routing import RouteCandidate, RoutingInput, RoutingProposal, RoutingSessionState


@runtime_checkable
class RoutingPolicy(Protocol):
    policy_id: str
    policy_revision: str

    async def propose(
        self,
        routing_input: RoutingInput,
        candidates: tuple[RouteCandidate, ...],
        state: RoutingSessionState,
    ) -> RoutingProposal:
        ...


@runtime_checkable
class RoutingStateStore(Protocol):
    async def read(self, session_id: str) -> RoutingSessionState:
        ...

    async def commit(
        self,
        session_id: str,
        *,
        expected_generation: int,
        state: RoutingSessionState,
    ) -> None:
        ...


__all__ = ["RoutingPolicy", "RoutingStateStore"]
