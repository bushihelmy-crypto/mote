"""Idempotent recovery for the two-owner Agent ingress protocol."""

from __future__ import annotations

from dataclasses import dataclass

from mote.contracts.agent.delivery import AgentDeliveryState
from mote.contracts.ports.agent.ingress import AgentIncarnationGenerationQuery
from mote.contracts.ports.runtime.lease import LeaseEpoch
from mote.orchestration.agents.messaging.durable import AgentDeliveryStore
from mote.orchestration.agents.turn_queue.model import TurnMutationDisposition, TurnQueueState
from mote.orchestration.agents.turn_queue.store import DurableTurnQueueStore


@dataclass(frozen=True, slots=True)
class AgentIngressReconcileResult:
    accepted_committed: int
    deliveries_acked: int
    terminals_committed: int
    owner_actions_required: int


class AgentIngressReconciler:
    def __init__(
        self,
        *,
        deliveries: AgentDeliveryStore,
        turns: DurableTurnQueueStore,
        generations: AgentIncarnationGenerationQuery,
        delivery_lease: LeaseEpoch,
        turn_lease: LeaseEpoch,
        scan_limit: int = 1_000,
    ) -> None:
        if type(scan_limit) is not int or not 1 <= scan_limit <= 10_000:
            raise ValueError("Agent ingress reconciliation scan is outside its bound")
        self._deliveries = deliveries
        self._turns = turns
        self._generations = generations
        self._delivery_lease = delivery_lease
        self._turn_lease = turn_lease
        self._scan_limit = scan_limit

    def reconcile(self) -> AgentIngressReconcileResult:
        delivery_by_id = {record.delivery_id: record for record in self._deliveries.records()}
        accepted = acked = terminals = owner_actions = 0
        for item in self._turns.load().items[: self._scan_limit]:
            batch = tuple(delivery_by_id.get(delivery_id) for delivery_id in item.identity.delivery_ids)
            if any(record is None for record in batch):
                owner_actions += 1
                continue
            records = tuple(record for record in batch if record is not None)
            if item.state is TurnQueueState.PREPARED:
                generation = self._generations.current_generation(item.identity.agent_id)
                self._deliveries.bind_to_turn(
                    item.identity.delivery_ids,
                    turn_request_id=item.identity.request_id,
                    target_generation=generation,
                    expected_payload_digest=item.payload_digest,
                    lease=self._delivery_lease,
                )
                receipt = self._turns.commit_acceptance(
                    request_id=item.identity.request_id,
                    expected_item_revision=item.revision,
                    lease=self._turn_lease,
                )
                if receipt.disposition is TurnMutationDisposition.APPLIED:
                    accepted += 1
                continue
            if item.state is TurnQueueState.EXECUTION_SETTLEMENT_PREPARED:
                if any(
                    record.state not in {AgentDeliveryState.BOUND_TO_TURN, AgentDeliveryState.ACKED}
                    for record in records
                ):
                    owner_actions += 1
                    continue
                for record in records:
                    if record.state is AgentDeliveryState.BOUND_TO_TURN:
                        self._deliveries.ack(record.delivery_id, record.target_generation, lease=self._delivery_lease)
                        acked += 1
                receipt = self._turns.commit_execution_settlement(
                    request_id=item.identity.request_id,
                    expected_item_revision=item.revision,
                    lease=self._turn_lease,
                )
                if receipt.disposition is TurnMutationDisposition.APPLIED:
                    terminals += 1
            elif item.state.terminal:
                for record in records:
                    if record.state is AgentDeliveryState.BOUND_TO_TURN:
                        self._deliveries.ack(record.delivery_id, record.target_generation, lease=self._delivery_lease)
                        acked += 1
        return AgentIngressReconcileResult(accepted, acked, terminals, owner_actions)


__all__ = ["AgentIngressReconcileResult", "AgentIngressReconciler"]
