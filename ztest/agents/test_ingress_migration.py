from __future__ import annotations

import json

from mote.contracts.agent.capacity import TurnCapacityPermitReceipt
from mote.contracts.clock import UNIX_UTC_CLOCK, AbsoluteInstant
from mote.contracts.conversation import UserMessage, dump_message
from mote.orchestration.agents.messaging.durable import AgentDeliveryStore
from mote.orchestration.agents.turn_queue.codec import TurnQueueSnapshot, decode_turn_queue, encode_turn_queue
from mote.orchestration.agents.turn_queue.model import TurnPriority, TurnQueueIdentity, TurnQueueItem, TurnQueueState
from mote.product.migrations.agent_ingress import migrate_agent_ingress_v1
from mote.runtime.control.leases import InMemoryLeaseCoordinator


def test_cross_store_v1_migration_binds_turn_and_removes_mailbox_truth(tmp_path) -> None:
    message = UserMessage(id="message-1", content="hello")
    delivery_id = AgentDeliveryStore.identity("agent-1", message)
    delivery = {
        "schema": "mote.agent-delivery-store/v1",
        "records": [
            {
                "delivery_id": delivery_id,
                "target_agent_id": "agent-1",
                "target_generation": 3,
                "mode": "trigger_turn",
                "message_payload": dump_message(message),
                "state": "claimed",
                "revision": 2,
                "fencing_token": 4,
                "reason": "",
            }
        ],
    }
    turn = TurnQueueItem(
        identity=TurnQueueIdentity("queue-1", "turn-1", "root-1", "/root/agent-1", "agent-1", (delivery_id,)),
        enqueue_sequence=1,
        config_generation=1,
        revision=1,
        priority=TurnPriority.NORMAL,
        state=TurnQueueState.ACCEPTED,
        accepted_at=AbsoluteInstant(1, UNIX_UTC_CLOCK, 1),
        deadline=None,
        attempt=0,
        maximum_attempts=3,
        next_eligible_at=None,
        payload_digest="placeholder",
    )
    turns = encode_turn_queue(TurnQueueSnapshot("queue-1", 1, 2, 8, (turn,)))
    turns["schema"] = "mote.agent-turn-queue/v1"
    for item in turns["items"]:
        del item["payload_digest"]
        del item["settlement_state"]
    residency = {
        "schema": "mote.agent-residency/v1",
        "identity": {},
        "source_session_revision": 1,
        "record_revision": 1,
        "materialization_fence": {},
        "state_snapshot": {},
        "message_buffer_snapshot": [],
        "lifecycle": "materialized",
        "install_fence": None,
        "mailbox_snapshot": {
            "schema": "mote.agent-mailbox/v1",
            "schema_version": 1,
            "owner_agent_id": "agent-1",
            "next_sequence": 2,
            "items": [
                {"sequence": 1, "delivery_id": delivery_id, "message": dump_message(message), "trigger_turn": True}
            ],
        },
    }
    (tmp_path / "agent-deliveries.json").write_text(json.dumps(delivery))
    (tmp_path / "agent-turn-queue.json").write_text(json.dumps(turns))
    (tmp_path / "agent-1.json").write_text(json.dumps(residency))

    receipt = migrate_agent_ingress_v1(tmp_path)
    assert (receipt.delivery_count, receipt.turn_count, receipt.residency_count) == (1, 1, 1)
    assert "mailbox_snapshot" not in json.loads((tmp_path / "agent-1.json").read_text())
    migrated_turn = decode_turn_queue(
        json.loads((tmp_path / "agent-turn-queue.json").read_text()), expected_queue_id="queue-1"
    )
    assert migrated_turn.items[0].payload_digest != "placeholder"
    leases = InMemoryLeaseCoordinator()
    lease = leases.acquire("delivery:root", "owner", 30)
    store = AgentDeliveryStore(tmp_path / "agent-deliveries.json", leases=leases, subject="delivery:root")
    record = store.records()[0]
    assert record.turn_request_id == "turn-1"
    assert record.state.value == "bound_to_turn"
    assert tuple(tmp_path.glob("*.v1-evidence-*"))
