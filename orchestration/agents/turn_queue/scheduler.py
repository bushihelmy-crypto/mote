"""Fenced durable claim owner for Agent turns."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from mote.contracts.agent.capacity import TurnCapacityPermitReceipt
from mote.contracts.agent.errors import AgentLimitReached
from mote.contracts.clock import AbsoluteInstant
from mote.contracts.ports.runtime.lease import LeaseEpoch
from mote.orchestration.agents.turn_queue.limiter import AgentExecutionGuard, AgentExecutionLimiter
from mote.orchestration.agents.turn_queue.model import (
    TurnMutationDisposition,
    TurnMutationReceipt,
    TurnQueueItem,
    TurnQueueState,
)
from mote.orchestration.agents.turn_queue.scheduling import TurnSchedulingConfig, choose_turn
from mote.orchestration.agents.turn_queue.store import DurableTurnQueueStore


class TurnClaimDisposition(StrEnum):
    CLAIMED = "claimed"
    NO_ELIGIBLE = "no_eligible"
    EXECUTION_BACKPRESSURE = "execution_backpressure"
    REVISION_CONFLICT = "revision_conflict"
    STALE_FENCE = "stale_fence"
    OWNER_LOST = "owner_lost"


@dataclass(frozen=True, slots=True)
class TurnRetryPolicy:
    base_delay_seconds: int = 1
    maximum_delay_seconds: int = 60

    def __post_init__(self) -> None:
        if type(self.base_delay_seconds) is not int or self.base_delay_seconds < 1:
            raise ValueError("turn retry base delay is invalid")
        if (
            type(self.maximum_delay_seconds) is not int
            or self.maximum_delay_seconds < self.base_delay_seconds
            or self.maximum_delay_seconds > 3600
        ):
            raise ValueError("turn retry maximum delay is invalid")

    def next_eligible(self, now: AbsoluteInstant, *, attempt: int) -> AbsoluteInstant:
        if type(attempt) is not int or attempt < 1:
            raise ValueError("turn retry attempt is invalid")
        delay = min(self.base_delay_seconds * (2 ** (attempt - 1)), self.maximum_delay_seconds)
        return AbsoluteInstant(
            now.schema_version,
            now.clock,
            now.epoch_nanoseconds + delay * 1_000_000_000,
        )


@dataclass(slots=True)
class ActiveTurnClaim:
    item: TurnQueueItem
    _guard: AgentExecutionGuard
    _released: bool = False

    @property
    def execution_permit_receipt(self) -> TurnCapacityPermitReceipt:
        return self._guard.receipt

    def _release_after_settlement(self) -> None:
        if not self._released:
            self._guard.release()
            self._released = True


@dataclass(frozen=True, slots=True)
class TurnClaimAttempt:
    disposition: TurnClaimDisposition
    claim: ActiveTurnClaim | None


class DurableTurnScheduler:
    def __init__(self, *, store: DurableTurnQueueStore, limiter: AgentExecutionLimiter) -> None:
        self._store = store
        self._limiter = limiter

    def claim_next(
        self,
        *,
        config: TurnSchedulingConfig,
        now: AbsoluteInstant,
        lease: LeaseEpoch,
        process_instance_id: str,
    ) -> TurnClaimAttempt:
        snapshot = self._store.load()
        decision = choose_turn(snapshot, config=config, now=now)
        if decision is None:
            return TurnClaimAttempt(TurnClaimDisposition.NO_ELIGIBLE, None)
        try:
            guard = self._limiter.guard()
        except AgentLimitReached:
            return TurnClaimAttempt(TurnClaimDisposition.EXECUTION_BACKPRESSURE, None)
        commit = self._store.claim(
            request_id=decision.item.identity.request_id,
            expected_queue_revision=snapshot.revision,
            expected_item_revision=decision.item.revision,
            scheduling=decision.scheduling,
            lease=lease,
            process_instance_id=process_instance_id,
            execution_permit_receipt=guard.receipt,
            claimed_at=now,
        )
        if commit.item is not None:
            return TurnClaimAttempt(
                TurnClaimDisposition.CLAIMED,
                ActiveTurnClaim(commit.item, guard),
            )
        guard.release()
        disposition = {
            TurnMutationDisposition.STALE_FENCE: TurnClaimDisposition.STALE_FENCE,
            TurnMutationDisposition.OWNER_LOST: TurnClaimDisposition.OWNER_LOST,
        }.get(commit.receipt.disposition, TurnClaimDisposition.REVISION_CONFLICT)
        return TurnClaimAttempt(disposition, None)

    def settle(
        self,
        claim: ActiveTurnClaim,
        *,
        succeeded: bool,
        reason: str,
        lease: LeaseEpoch,
    ) -> TurnMutationReceipt:
        binding = claim.item.claim
        if binding is None:
            raise ValueError("active turn claim has no durable binding")
        receipt = self._store.settle_claim(
            request_id=claim.item.identity.request_id,
            expected_item_revision=claim.item.revision,
            terminal_state=TurnQueueState.SUCCEEDED if succeeded else TurnQueueState.FAILED,
            terminal_reason=reason,
            lease=lease,
            process_instance_id=binding.process_instance_id,
            execution_permit_receipt=claim.execution_permit_receipt,
        )
        if receipt.disposition in {
            TurnMutationDisposition.APPLIED,
            TurnMutationDisposition.ALREADY_TERMINAL,
            TurnMutationDisposition.STALE_FENCE,
            TurnMutationDisposition.OWNER_LOST,
        }:
            claim._release_after_settlement()
        return receipt

    def retry(
        self,
        claim: ActiveTurnClaim,
        *,
        reason: str,
        now: AbsoluteInstant,
        lease: LeaseEpoch,
        policy: TurnRetryPolicy = TurnRetryPolicy(),
    ) -> TurnMutationReceipt:
        binding = claim.item.claim
        if binding is None:
            raise ValueError("active turn claim has no durable binding")
        receipt = self._store.retry_claim(
            request_id=claim.item.identity.request_id,
            expected_item_revision=claim.item.revision,
            terminal_reason=reason,
            next_eligible_at=policy.next_eligible(now, attempt=claim.item.attempt),
            lease=lease,
            process_instance_id=binding.process_instance_id,
            execution_permit_receipt=claim.execution_permit_receipt,
        )
        if receipt.disposition in {
            TurnMutationDisposition.APPLIED,
            TurnMutationDisposition.ALREADY_TERMINAL,
            TurnMutationDisposition.STALE_FENCE,
            TurnMutationDisposition.OWNER_LOST,
        }:
            claim._release_after_settlement()
        return receipt

    def cancel_unclaimed(
        self,
        item: TurnQueueItem,
        *,
        reason: str,
        lease: LeaseEpoch,
    ) -> TurnMutationReceipt:
        return self._store.settle_unclaimed(
            request_id=item.identity.request_id,
            expected_item_revision=item.revision,
            terminal_state=TurnQueueState.CANCELLED,
            terminal_reason=reason,
            lease=lease,
        )

    def expire_unclaimed(
        self,
        item: TurnQueueItem,
        *,
        now: AbsoluteInstant,
        lease: LeaseEpoch,
    ) -> TurnMutationReceipt:
        return self._store.settle_unclaimed(
            request_id=item.identity.request_id,
            expected_item_revision=item.revision,
            terminal_state=TurnQueueState.EXPIRED,
            terminal_reason="deadline_elapsed_before_claim",
            lease=lease,
            now=now,
        )

    def reconcile_expired(
        self,
        *,
        now: AbsoluteInstant,
        lease: LeaseEpoch,
    ) -> tuple[TurnMutationReceipt, ...]:
        snapshot = self._store.load()
        due = tuple(
            item
            for item in snapshot.items
            if item.eligible and item.deadline is not None and now.is_at_or_after(item.deadline)
        )
        return tuple(self.expire_unclaimed(item, now=now, lease=lease) for item in due)

    def cancel_root(
        self,
        root_id: str,
        *,
        reason: str,
        lease: LeaseEpoch,
    ) -> tuple[TurnMutationReceipt, ...]:
        if type(root_id) is not str or not root_id:
            raise ValueError("turn cancellation root identity is invalid")
        snapshot = self._store.load()
        queued = tuple(item for item in snapshot.items if item.eligible and item.identity.root_id == root_id)
        return tuple(self.cancel_unclaimed(item, reason=reason, lease=lease) for item in queued)

    def cancel_agent(
        self,
        agent_id: str,
        *,
        reason: str,
        lease: LeaseEpoch,
    ) -> tuple[TurnMutationReceipt, ...]:
        if type(agent_id) is not str or not agent_id:
            raise ValueError("turn cancellation Agent identity is invalid")
        queued = tuple(
            item for item in self._store.load().items if item.eligible and item.identity.agent_id == agent_id
        )
        return tuple(self.cancel_unclaimed(item, reason=reason, lease=lease) for item in queued)

    def settle_lost_claim(
        self,
        item: TurnQueueItem,
        *,
        lease: LeaseEpoch,
    ) -> TurnMutationReceipt:
        binding = item.claim
        if binding is None:
            raise ValueError("lost turn settlement requires a claimed item")
        return self._store.settle_lost_claim(
            request_id=item.identity.request_id,
            expected_item_revision=item.revision,
            prior_scheduler_fencing_token=binding.scheduler_fencing_token,
            terminal_reason="execution_owner_lost",
            lease=lease,
        )


__all__ = [
    "ActiveTurnClaim",
    "DurableTurnScheduler",
    "TurnClaimAttempt",
    "TurnClaimDisposition",
    "TurnRetryPolicy",
]
