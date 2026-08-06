"""Guarded Session commands for PendingAct execution ownership."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from mote.contracts.events.pending_act import (
    PendingActClaimAcquiredEvent,
    PendingActClaimReleasedEvent,
    PendingActClaimRenewedEvent,
    PendingActClaimTakenOverEvent,
)
from mote.contracts.execution.pending_act_claim import (
    PendingActClaimId,
    PendingActExecutionClaim,
    PendingActInvokePermit,
)
from mote.contracts.execution.pending_act_identity import PendingActFrontierId
from mote.contracts.ports.events.journal import AppendResult, StreamWriterFence
from mote.contracts.ports.session.facts import GuardedSessionFactBatch, GuardedSessionFactSink
from mote.contracts.tool.identity import ToolInvocationId
from mote.runtime.session.projection import SessionLiveProjection, SessionProjectionState


class PendingActClaimService:
    def __init__(self, projection: SessionLiveProjection, sink: GuardedSessionFactSink) -> None:
        self._projection = projection
        self._sink = sink

    async def acquire(
        self,
        frontier_id: PendingActFrontierId,
        owner_id: str,
        incarnation_id: str,
        *,
        acquired_at: datetime,
        expires_at: datetime,
        expected_stream_version: int,
        writer: StreamWriterFence,
    ) -> PendingActExecutionClaim:
        state = self._snapshot(expected_stream_version)
        if frontier_id not in state.pending_act_by_id or frontier_id in state.claim_by_frontier_id:
            raise ValueError("PendingAct cannot be acquired")
        claim = PendingActExecutionClaim(
            PendingActClaimId(uuid4().hex),
            frontier_id,
            owner_id,
            incarnation_id,
            0,
            1,
            acquired_at,
            expires_at,
        )
        await self._commit(PendingActClaimAcquiredEvent(claim), expected_stream_version, writer)
        return claim

    async def renew(
        self,
        claim: PendingActExecutionClaim,
        *,
        expires_at: datetime,
        expected_stream_version: int,
        writer: StreamWriterFence,
    ) -> PendingActExecutionClaim:
        current = self._require_current(claim, expected_stream_version)
        renewed = PendingActExecutionClaim(
            current.claim_id,
            current.frontier_id,
            current.owner_id,
            current.incarnation_id,
            current.claim_revision + 1,
            current.fencing_token,
            current.acquired_at,
            expires_at,
        )
        await self._commit(PendingActClaimRenewedEvent(renewed), expected_stream_version, writer)
        return renewed

    async def takeover(
        self,
        frontier_id: PendingActFrontierId,
        owner_id: str,
        incarnation_id: str,
        *,
        acquired_at: datetime,
        expires_at: datetime,
        expected_stream_version: int,
        writer: StreamWriterFence,
    ) -> PendingActExecutionClaim:
        state = self._snapshot(expected_stream_version)
        current = state.claim_by_frontier_id.get(frontier_id)
        if current is None or acquired_at < current.expires_at:
            raise ValueError("PendingAct claim is not eligible for takeover")
        taken = PendingActExecutionClaim(
            PendingActClaimId(uuid4().hex),
            frontier_id,
            owner_id,
            incarnation_id,
            current.claim_revision + 1,
            current.fencing_token + 1,
            acquired_at,
            expires_at,
        )
        await self._commit(PendingActClaimTakenOverEvent(taken), expected_stream_version, writer)
        return taken

    async def release(
        self,
        claim: PendingActExecutionClaim,
        *,
        expected_stream_version: int,
        writer: StreamWriterFence,
    ) -> AppendResult:
        current = self._require_current(claim, expected_stream_version)
        return await self._commit(
            PendingActClaimReleasedEvent(
                current.frontier_id,
                current.claim_id,
                current.claim_revision + 1,
                current.fencing_token,
            ),
            expected_stream_version,
            writer,
        )

    def begin_invoke(
        self,
        claim: PendingActExecutionClaim,
        invocation_id: ToolInvocationId,
        *,
        frontier_revision: int,
        expected_stream_version: int,
        at: datetime,
    ) -> PendingActInvokePermit:
        current = self._require_current(claim, expected_stream_version)
        frontier = self._snapshot(expected_stream_version).pending_act_by_id.get(current.frontier_id)
        if frontier is None or frontier.revision != frontier_revision or at >= current.expires_at:
            raise ValueError("PendingAct invoke claim is stale")
        if invocation_id not in {action.invocation_id for action in frontier.actions}:
            raise ValueError("invoke identity is not in the claimed frontier")
        return PendingActInvokePermit(
            current.claim_id,
            current.frontier_id,
            current.owner_id,
            current.incarnation_id,
            current.claim_revision,
            current.fencing_token,
            frontier_revision,
            invocation_id,
        )

    def _require_current(
        self, claim: PendingActExecutionClaim, expected_stream_version: int
    ) -> PendingActExecutionClaim:
        current = self._snapshot(expected_stream_version).claim_by_frontier_id.get(claim.frontier_id)
        if current is None or current != claim:
            raise ValueError("PendingAct claim is stale")
        return current

    def _snapshot(self, expected_stream_version: int) -> SessionProjectionState:
        state = self._projection.snapshot()
        if state.through_sequence != expected_stream_version:
            raise ValueError("claim snapshot is not at expected stream version")
        return state

    async def _commit(
        self,
        event: (
            PendingActClaimAcquiredEvent
            | PendingActClaimRenewedEvent
            | PendingActClaimTakenOverEvent
            | PendingActClaimReleasedEvent
        ),
        version: int,
        writer: StreamWriterFence,
    ) -> AppendResult:
        return await self._sink.commit_guarded(GuardedSessionFactBatch((event,), version, writer))


__all__ = ["PendingActClaimService"]
