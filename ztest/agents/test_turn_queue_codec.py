from __future__ import annotations

from copy import deepcopy

import pytest

from mote.contracts.agent.capacity import TurnCapacityPermitReceipt
from mote.contracts.clock import UNIX_UTC_CLOCK, AbsoluteInstant, ClockIdentity
from mote.orchestration.agents.turn_queue.codec import TurnQueueSnapshot, decode_turn_queue, encode_turn_queue
from mote.orchestration.agents.turn_queue.model import (
    TurnClaimBinding,
    TurnPriority,
    TurnQueueIdentity,
    TurnQueueItem,
    TurnQueueState,
)


def _instant(value: int, *, clock: ClockIdentity = UNIX_UTC_CLOCK) -> AbsoluteInstant:
    return AbsoluteInstant(1, clock, value)


def _item(
    request: str = "request-1",
    *,
    sequence: int = 1,
    state: TurnQueueState = TurnQueueState.ACCEPTED,
) -> TurnQueueItem:
    revision = 2 if state is TurnQueueState.CLAIMED else 1
    claim = None
    if state is TurnQueueState.CLAIMED:
        claim = TurnClaimBinding(
            "queue-1", "owner-1", 3, "process-1", TurnCapacityPermitReceipt("permit-4"), revision, _instant(12)
        )
    return TurnQueueItem(
        identity=TurnQueueIdentity("queue-1", request, "root-1", "subtree-1", "agent-1", (f"delivery-{sequence}",)),
        enqueue_sequence=sequence,
        config_generation=1,
        revision=revision,
        priority=TurnPriority.HIGH,
        state=state,
        accepted_at=_instant(10),
        deadline=_instant(30),
        attempt=1 if state is TurnQueueState.CLAIMED else 0,
        maximum_attempts=3,
        next_eligible_at=None,
        payload_digest="digest-1",
        claim=claim,
    )


def _encoded() -> dict[str, object]:
    return encode_turn_queue(TurnQueueSnapshot("queue-1", 2, 2, 8, (_item(),)))


def _item_record(raw: dict[str, object]) -> dict[str, object]:
    items = raw["items"]
    assert isinstance(items, list)
    item = items[0]
    assert isinstance(item, dict)
    return item


def test_turn_queue_codec_round_trips_claim_binding() -> None:
    snapshot = TurnQueueSnapshot("queue-1", 3, 2, 8, (_item(state=TurnQueueState.CLAIMED),))
    assert decode_turn_queue(encode_turn_queue(snapshot), expected_queue_id="queue-1") == snapshot


@pytest.mark.parametrize("field", ["schema", "capacity"])
def test_turn_queue_codec_rejects_missing_and_extra_envelope_fields(field: str) -> None:
    missing = _encoded()
    del missing[field]
    with pytest.raises(ValueError, match="fields are not canonical"):
        decode_turn_queue(missing, expected_queue_id="queue-1")
    extra = _encoded()
    extra["unknown"] = 1
    with pytest.raises(ValueError, match="fields are not canonical"):
        decode_turn_queue(extra, expected_queue_id="queue-1")


def test_turn_queue_codec_rejects_unknown_schema_and_queue() -> None:
    raw = _encoded()
    raw["schema"] = "mote.agent-turn-queue/v9"
    with pytest.raises(ValueError, match="unsupported"):
        decode_turn_queue(raw, expected_queue_id="queue-1")
    with pytest.raises(ValueError, match="identity mismatch"):
        decode_turn_queue(_encoded(), expected_queue_id="queue-2")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("priority", True, "priority primitive"),
        ("revision", True, "revision primitive"),
        ("priority", 99, "priority is unsupported"),
        ("state", "parked", "state is unsupported"),
    ],
)
def test_turn_queue_codec_rejects_invalid_item_primitives(field: str, value: object, message: str) -> None:
    raw = _encoded()
    _item_record(raw)[field] = value
    with pytest.raises(ValueError, match=message):
        decode_turn_queue(raw, expected_queue_id="queue-1")


def test_turn_queue_codec_rejects_duplicate_request_and_unordered_sequence() -> None:
    raw = _encoded()
    item = _item_record(raw)
    items = raw["items"]
    assert isinstance(items, list)
    duplicate = deepcopy(item)
    duplicate["enqueue_sequence"] = 2
    items.append(duplicate)
    raw["next_enqueue_sequence"] = 3
    with pytest.raises(ValueError, match="request identities"):
        decode_turn_queue(raw, expected_queue_id="queue-1")

    raw = _encoded()
    items = raw["items"]
    assert isinstance(items, list)
    second = encode_turn_queue(TurnQueueSnapshot("queue-1", 1, 3, 8, (_item("request-2", sequence=2),)))["items"]
    assert isinstance(second, list)
    items.insert(0, second[0])
    raw["next_enqueue_sequence"] = 3
    with pytest.raises(ValueError, match="unique and ordered"):
        decode_turn_queue(raw, expected_queue_id="queue-1")


def test_turn_queue_codec_rejects_claim_revision_clock_and_terminal_reason_errors() -> None:
    claimed = encode_turn_queue(TurnQueueSnapshot("queue-1", 3, 2, 8, (_item(state=TurnQueueState.CLAIMED),)))
    claim = _item_record(claimed)["claim"]
    assert isinstance(claim, dict)
    claim["queue_revision"] = 1
    with pytest.raises(ValueError, match="current queue revision"):
        decode_turn_queue(claimed, expected_queue_id="queue-1")

    wrong_clock = _encoded()
    _item_record(wrong_clock)["deadline"] = _instant(30, clock=ClockIdentity(1, "other")).to_dict()
    with pytest.raises(ValueError, match="clock identity mismatch"):
        decode_turn_queue(wrong_clock, expected_queue_id="queue-1")

    terminal = _encoded()
    record = _item_record(terminal)
    record["state"] = TurnQueueState.EXPIRED.value
    with pytest.raises(ValueError, match="terminal turn"):
        decode_turn_queue(terminal, expected_queue_id="queue-1")
