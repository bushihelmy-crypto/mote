from __future__ import annotations

import hashlib

from mote.contracts.agent.capacity import TurnCapacityPermitReceipt
from mote.contracts.agent.delivery import AgentDeliveryState
from mote.contracts.clock import UNIX_UTC_CLOCK, AbsoluteInstant
from mote.contracts.conversation import UserMessage
from mote.orchestration.agents.ingress import AgentIngressReconciler
from mote.orchestration.agents.messaging.durable import AgentDeliveryStore
from mote.orchestration.agents.turn_queue.model import (
    EMPTY_TURN_SCHEDULING_STATE,
    TurnAcceptanceRequest,
    TurnPriority,
    TurnQueueIdentity,
    TurnQueueState,
)
from mote.orchestration.agents.turn_queue.store import DurableTurnQueueStore
from mote.runtime.control.leases import InMemoryLeaseCoordinator


class _Generations:
    def current_generation(self, agent_id: str) -> int:
        assert agent_id == "agent-1"
        return 3


def _instant(value: int) -> AbsoluteInstant:
    return AbsoluteInstant(1, UNIX_UTC_CLOCK, value)


def test_reconcile_closes_prepare_bind_commit_and_settlement_ack_terminal(tmp_path) -> None:
    leases = InMemoryLeaseCoordinator(clock=lambda: 1.0)
    delivery_lease = leases.acquire("delivery:root", "delivery-owner", 30)
    turn_lease = leases.acquire("turn:root", "turn-owner", 30)
    deliveries = AgentDeliveryStore(tmp_path / "deliveries.json", leases=leases, subject="delivery:root")
    turns = DurableTurnQueueStore(tmp_path / "turns.json", queue_id="queue-1", capacity=8, lease_coordinator=leases)
    record = deliveries.accept(
        "agent-1", UserMessage(id="message-1", content="hello"), "trigger_turn", lease=delivery_lease
    )
    batch_digest = hashlib.sha256(record.payload_digest.encode()).hexdigest()
    prepared = turns.prepare_acceptance(
        TurnAcceptanceRequest(
            TurnQueueIdentity("queue-1", "turn-1", "root-1", "/root/agent-1", "agent-1", (record.delivery_id,)),
            1,
            TurnPriority.NORMAL,
            _instant(1),
            None,
            3,
            batch_digest,
        ),
        lease=turn_lease,
    )
    assert prepared.revision == 1
    reconciler = AgentIngressReconciler(
        deliveries=deliveries,
        turns=turns,
        generations=_Generations(),
        delivery_lease=delivery_lease,
        turn_lease=turn_lease,
    )
    first = reconciler.reconcile()
    assert first.accepted_committed == 1
    assert turns.load().items[0].state is TurnQueueState.ACCEPTED
    assert deliveries.records()[0].state is AgentDeliveryState.BOUND_TO_TURN

    snapshot = turns.load()
    item = snapshot.items[0]
    permit = TurnCapacityPermitReceipt("permit-1")
    claim = turns.claim(
        request_id="turn-1",
        expected_queue_revision=snapshot.revision,
        expected_item_revision=item.revision,
        scheduling=EMPTY_TURN_SCHEDULING_STATE,
        lease=turn_lease,
        process_instance_id="process-1",
        execution_permit_receipt=permit,
        claimed_at=_instant(2),
    )
    assert claim.item is not None
    settlement = turns.prepare_execution_settlement(
        request_id="turn-1",
        expected_item_revision=claim.item.revision,
        terminal_state=TurnQueueState.SUCCEEDED,
        terminal_reason="completed",
        lease=turn_lease,
        process_instance_id="process-1",
        execution_permit_receipt=permit,
    )
    assert settlement.state is TurnQueueState.EXECUTION_SETTLEMENT_PREPARED
    second = reconciler.reconcile()
    assert second.deliveries_acked == 1 and second.terminals_committed == 1
    assert turns.load().items[0].state is TurnQueueState.SUCCEEDED
    assert deliveries.records()[0].state is AgentDeliveryState.ACKED
