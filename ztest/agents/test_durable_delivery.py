from __future__ import annotations

import hashlib
import json

import pytest

from mote.contracts.agent.delivery import AgentDeliveryState
from mote.contracts.conversation import UserMessage
from mote.orchestration.agents.messaging.durable import AgentDeliveryStore
from mote.runtime.control.leases import InMemoryLeaseCoordinator


def _store(tmp_path):
    leases = InMemoryLeaseCoordinator()
    lease = leases.acquire("delivery:root", "owner", 30)
    return AgentDeliveryStore(tmp_path / "delivery.json", leases=leases, subject="delivery:root"), lease


def _bind(store, record, generation, lease, *, turn_request_id="turn-1"):
    digest = hashlib.sha256(record.payload_digest.encode()).hexdigest()
    return store.bind_to_turn(
        (record.delivery_id,),
        turn_request_id=turn_request_id,
        target_generation=generation,
        expected_payload_digest=digest,
        lease=lease,
    )[0]


def test_accept_is_durable_and_idempotent_per_target(tmp_path):
    store, lease = _store(tmp_path)
    message = UserMessage(id="message-1", content="hello")
    first = store.accept("agent-a", message, "trigger_turn", lease=lease)
    duplicate = store.accept("agent-a", message, "trigger_turn", lease=lease)
    other_target = store.accept("agent-b", message, "trigger_turn", lease=lease)
    assert first == duplicate
    assert other_target.delivery_id != first.delivery_id
    reopened = AgentDeliveryStore(store._path, leases=store._leases, subject=store._subject)
    assert {record.delivery_id for record in reopened.pending()} == {first.delivery_id, other_target.delivery_id}


def test_turn_binding_is_stable_and_stale_ack_fails(tmp_path):
    store, lease = _store(tmp_path)
    accepted = store.accept("agent", UserMessage(id="m", content="x"), "trigger_turn", lease=lease)
    bound = _bind(store, accepted, 4, lease)
    assert bound.target_generation == 4
    with pytest.raises(RuntimeError, match="another turn"):
        _bind(store, accepted, 5, lease, turn_request_id="turn-2")
    try:
        store.ack(accepted.delivery_id, 3, lease=lease)
    except RuntimeError:
        pass
    else:
        raise AssertionError("stale incarnation ack must fail")
    assert store.ack(accepted.delivery_id, 4, lease=lease).state is AgentDeliveryState.ACKED


def test_terminal_target_dead_letters_each_unsettled_delivery(tmp_path):
    store, lease = _store(tmp_path)
    for sequence in range(2):
        store.accept("agent", UserMessage(id=f"m-{sequence}", content="x"), "queue_only", lease=lease)
    store.dead_letter_target("agent", "logical_agent_terminal", lease=lease)
    records = store.records()
    assert all(record.state is AgentDeliveryState.DEAD_LETTER for record in records)
    assert all(record.reason == "logical_agent_terminal" for record in records)


def test_identity_binds_source_target_and_dedupe_key(tmp_path):
    store, lease = _store(tmp_path)
    left = UserMessage(id="same", sent_from="left", content="x")
    right = UserMessage(id="same", sent_from="right", content="x")
    identities = {
        store.accept("a", left, "queue_only", lease=lease).delivery_id,
        store.accept("b", left, "queue_only", lease=lease).delivery_id,
        store.accept("a", right, "queue_only", lease=lease).delivery_id,
    }
    assert len(identities) == 3


def test_strict_decoder_rejects_poison_payload(tmp_path):
    store, lease = _store(tmp_path)
    store.accept("agent", UserMessage(id="m", content="x"), "queue_only", lease=lease)
    envelope = json.loads(store._path.read_text(encoding="utf-8"))
    envelope["records"][0]["message_payload"] = '{"unknown":"shape"}'
    store._path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(ValueError, match="message payload"):
        store.pending()


def test_stale_delivery_owner_cannot_mutate_after_takeover(tmp_path):
    now = [10.0]
    leases = InMemoryLeaseCoordinator(clock=lambda: now[0])
    old = leases.acquire("delivery:root", "old", 1)
    store = AgentDeliveryStore(tmp_path / "delivery.json", leases=leases, subject="delivery:root")
    accepted = store.accept("agent", UserMessage(id="m", content="x"), "queue_only", lease=old)
    now[0] = 12.0
    current = leases.acquire("delivery:root", "current", 30)
    with pytest.raises(Exception):
        _bind(store, accepted, 1, old)
    assert _bind(store, accepted, 1, current).state is AgentDeliveryState.BOUND_TO_TURN
