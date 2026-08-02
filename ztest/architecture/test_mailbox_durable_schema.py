from __future__ import annotations

import json
from copy import deepcopy

import pytest

from mote.contracts.conversation import UserMessage
from mote.orchestration.agents.messaging.mailbox import DeliveryMode, Mailbox


def _dump() -> dict:
    mailbox = Mailbox("agent-1")
    mailbox.enqueue(UserMessage("first"), mode=DeliveryMode.QUEUE_ONLY)
    mailbox.enqueue(UserMessage("second"), mode=DeliveryMode.TRIGGER_TURN)
    return mailbox.dump()


def test_mailbox_owner_sequence_delivery_and_order_roundtrip() -> None:
    payload = _dump()
    restored = Mailbox.load(payload, expected_owner_agent_id="agent-1")
    assert restored.owner_agent_id == "agent-1"
    assert [message.content for message in restored.drain_for_turn()] == ["first", "second"]
    assert [item["sequence"] for item in payload["items"]] == [1, 2]
    assert [item["delivery_id"] for item in payload["items"]] == [
        str(UserMessage.load(item["message"]).id) for item in payload["items"]
    ]


def test_mailbox_rejects_unknown_version_owner_mismatch_and_extra_fields() -> None:
    payload = _dump()
    future = deepcopy(payload)
    future["schema_version"] = 2
    with pytest.raises(ValueError, match="version"):
        Mailbox.load(future, expected_owner_agent_id="agent-1")
    with pytest.raises(ValueError, match="owner identity mismatch"):
        Mailbox.load(payload, expected_owner_agent_id="agent-2")
    extra = deepcopy(payload)
    extra["extra"] = True
    with pytest.raises(ValueError, match="envelope fields"):
        Mailbox.load(extra, expected_owner_agent_id="agent-1")


def test_mailbox_rejects_duplicate_or_unordered_sequence() -> None:
    duplicate = _dump()
    duplicate["items"][1]["sequence"] = 1
    with pytest.raises(ValueError, match="unique and ordered"):
        Mailbox.load(duplicate, expected_owner_agent_id="agent-1")
    out_of_range = _dump()
    out_of_range["items"][1]["sequence"] = out_of_range["next_sequence"]
    with pytest.raises(ValueError, match="sequence is invalid"):
        Mailbox.load(out_of_range, expected_owner_agent_id="agent-1")


def test_mailbox_rejects_delivery_identity_and_boolean_coercion() -> None:
    delivery = _dump()
    delivery["items"][0]["delivery_id"] = "wrong"
    with pytest.raises(ValueError, match="delivery identity mismatch"):
        Mailbox.load(delivery, expected_owner_agent_id="agent-1")
    trigger = _dump()
    trigger["items"][0]["trigger_turn"] = 1
    with pytest.raises(ValueError, match="must be a boolean"):
        Mailbox.load(trigger, expected_owner_agent_id="agent-1")


def test_noncanonical_mailbox_shape_fails_closed() -> None:
    message = UserMessage("noncanonical")
    legacy = [{"message": message.dump(), "trigger_turn": False}]
    with pytest.raises(ValueError, match="envelope fields"):
        Mailbox.load(legacy, expected_owner_agent_id="agent-1")


@pytest.mark.parametrize(
    "mutate",
    (
        lambda message: message.__setitem__("extra", "rejected"),
        lambda message: message.__setitem__("content", 1),
        lambda message: message.__setitem__("send_to", ["", "duplicate", "duplicate"]),
        lambda message: message.__setitem__("metadata", ["not", "an", "object"]),
    ),
)
def test_mailbox_inner_message_decoder_fails_closed(mutate) -> None:
    payload = _dump()
    message = json.loads(payload["items"][0]["message"])
    mutate(message)
    payload["items"][0]["message"] = json.dumps(message)
    with pytest.raises((TypeError, ValueError)):
        Mailbox.load(payload, expected_owner_agent_id="agent-1")
