"""Strict v2 codec for authoritative Agent turn state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias, cast

from mote.contracts.agent.capacity import TurnCapacityPermitReceipt
from mote.contracts.clock import AbsoluteInstant
from mote.orchestration.agents.turn_queue.model import (
    TurnClaimBinding,
    TurnPriority,
    TurnQueueIdentity,
    TurnQueueItem,
    TurnQueueState,
    TurnSchedulingCursor,
    TurnSchedulingDeficit,
    TurnSchedulingState,
    TurnSubtreeCursor,
)

TURN_QUEUE_SCHEMA = "mote.agent-turn-queue/v2"

JsonObject: TypeAlias = dict[str, object]

_ENVELOPE_FIELDS = {"schema", "queue_id", "revision", "next_enqueue_sequence", "capacity", "scheduling", "items"}
_SCHEDULING_FIELDS = {"config_generation", "root_cursor", "subtree_cursors", "deficits"}
_SUBTREE_CURSOR_FIELDS = {"root_id", "subtree_id"}
_DEFICIT_FIELDS = {"root_id", "subtree_id", "units"}
_ITEM_FIELDS = {
    "identity",
    "enqueue_sequence",
    "config_generation",
    "revision",
    "priority",
    "state",
    "accepted_at",
    "deadline",
    "attempt",
    "maximum_attempts",
    "next_eligible_at",
    "claim",
    "payload_digest",
    "settlement_state",
    "terminal_reason",
}
_IDENTITY_FIELDS = {"queue_id", "request_id", "root_id", "subtree_id", "agent_id", "delivery_ids"}
_CLAIM_FIELDS = {
    "scheduler_subject",
    "scheduler_owner_id",
    "scheduler_fencing_token",
    "process_instance_id",
    "execution_permit_receipt",
    "queue_revision",
    "claimed_at",
}


@dataclass(frozen=True, slots=True)
class TurnQueueSnapshot:
    queue_id: str
    revision: int
    next_enqueue_sequence: int
    capacity: int
    items: tuple[TurnQueueItem, ...]
    scheduling: TurnSchedulingState = TurnSchedulingState(0, TurnSchedulingCursor(None, ()), ())

    def __post_init__(self) -> None:
        if type(self.queue_id) is not str or not self.queue_id:
            raise ValueError("turn queue snapshot identity is invalid")
        for name in ("revision", "next_enqueue_sequence", "capacity"):
            value = getattr(self, name)
            minimum = 0 if name == "revision" else 1
            if type(value) is not int or value < minimum:
                raise ValueError(f"turn queue snapshot {name} is invalid")
        if not isinstance(self.items, tuple) or not all(isinstance(item, TurnQueueItem) for item in self.items):
            raise TypeError("turn queue snapshot items must be a typed tuple")
        if any(item.identity.queue_id != self.queue_id for item in self.items):
            raise ValueError("turn queue item belongs to another queue")
        sequences = tuple(item.enqueue_sequence for item in self.items)
        if sequences != tuple(sorted(sequences)) or len(sequences) != len(set(sequences)):
            raise ValueError("turn queue enqueue sequences must be unique and ordered")
        if sequences and (sequences[-1] >= self.next_enqueue_sequence):
            raise ValueError("turn queue next sequence does not follow existing items")
        requests = tuple(item.identity.request_id for item in self.items)
        if len(requests) != len(set(requests)):
            raise ValueError("turn queue request identities must be unique")
        if not isinstance(self.scheduling, TurnSchedulingState):
            raise TypeError("turn queue scheduling state is invalid")


def encode_turn_queue(snapshot: TurnQueueSnapshot) -> dict[str, object]:
    return {
        "schema": TURN_QUEUE_SCHEMA,
        "queue_id": snapshot.queue_id,
        "revision": snapshot.revision,
        "next_enqueue_sequence": snapshot.next_enqueue_sequence,
        "capacity": snapshot.capacity,
        "scheduling": _encode_scheduling(snapshot.scheduling),
        "items": [_encode_item(item) for item in snapshot.items],
    }


def decode_turn_queue(raw: object, *, expected_queue_id: str) -> TurnQueueSnapshot:
    value = _mapping(raw, _ENVELOPE_FIELDS, "turn queue envelope")
    if value["schema"] != TURN_QUEUE_SCHEMA:
        raise ValueError("turn queue schema is unsupported")
    queue_id = _string(value["queue_id"], "queue_id")
    if queue_id != expected_queue_id:
        raise ValueError("turn queue identity mismatch")
    revision = _integer(value["revision"], "revision", minimum=0)
    next_sequence = _integer(value["next_enqueue_sequence"], "next_enqueue_sequence", minimum=1)
    capacity = _integer(value["capacity"], "capacity", minimum=1)
    raw_items = value["items"]
    if type(raw_items) is not list:
        raise ValueError("turn queue items must be an array")
    return TurnQueueSnapshot(
        queue_id=queue_id,
        revision=revision,
        next_enqueue_sequence=next_sequence,
        capacity=capacity,
        items=tuple(_decode_item(item) for item in raw_items),
        scheduling=_decode_scheduling(value["scheduling"]),
    )


def _encode_scheduling(state: TurnSchedulingState) -> dict[str, object]:
    return {
        "config_generation": state.config_generation,
        "root_cursor": state.cursor.root_id,
        "subtree_cursors": [
            {"root_id": cursor.root_id, "subtree_id": cursor.subtree_id} for cursor in state.cursor.subtrees
        ],
        "deficits": [
            {"root_id": value.root_id, "subtree_id": value.subtree_id, "units": value.units} for value in state.deficits
        ],
    }


def _decode_scheduling(raw: object) -> TurnSchedulingState:
    value = _mapping(raw, _SCHEDULING_FIELDS, "turn scheduling state")
    root_cursor = _optional_string(value["root_cursor"], "root_cursor")
    raw_cursors = value["subtree_cursors"]
    if type(raw_cursors) is not list:
        raise ValueError("turn subtree cursors must be an array")
    subtree_cursors: list[TurnSubtreeCursor] = []
    for raw_cursor in raw_cursors:
        subtree = _mapping(raw_cursor, _SUBTREE_CURSOR_FIELDS, "turn subtree cursor")
        subtree_cursors.append(
            TurnSubtreeCursor(
                _string(subtree["root_id"], "subtree cursor root_id"),
                _string(subtree["subtree_id"], "subtree cursor subtree_id"),
            )
        )
    raw_deficits = value["deficits"]
    if type(raw_deficits) is not list:
        raise ValueError("turn scheduling deficits must be an array")
    deficits: list[TurnSchedulingDeficit] = []
    for raw_deficit in raw_deficits:
        deficit = _mapping(raw_deficit, _DEFICIT_FIELDS, "turn scheduling deficit")
        deficits.append(
            TurnSchedulingDeficit(
                root_id=_string(deficit["root_id"], "deficit root_id"),
                subtree_id=_optional_string(deficit["subtree_id"], "deficit subtree_id"),
                units=_integer(deficit["units"], "deficit units", minimum=0),
            )
        )
    return TurnSchedulingState(
        config_generation=_integer(value["config_generation"], "config_generation", minimum=0),
        cursor=TurnSchedulingCursor(root_cursor, tuple(subtree_cursors)),
        deficits=tuple(deficits),
    )


def _encode_item(item: TurnQueueItem) -> dict[str, object]:
    identity = item.identity
    return {
        "identity": {
            "queue_id": identity.queue_id,
            "request_id": identity.request_id,
            "root_id": identity.root_id,
            "subtree_id": identity.subtree_id,
            "agent_id": identity.agent_id,
            "delivery_ids": list(identity.delivery_ids),
        },
        "enqueue_sequence": item.enqueue_sequence,
        "config_generation": item.config_generation,
        "revision": item.revision,
        "priority": int(item.priority),
        "state": item.state.value,
        "accepted_at": item.accepted_at.to_dict(),
        "deadline": None if item.deadline is None else item.deadline.to_dict(),
        "attempt": item.attempt,
        "maximum_attempts": item.maximum_attempts,
        "next_eligible_at": None if item.next_eligible_at is None else item.next_eligible_at.to_dict(),
        "claim": None if item.claim is None else _encode_claim(item.claim),
        "payload_digest": item.payload_digest,
        "settlement_state": None if item.settlement_state is None else item.settlement_state.value,
        "terminal_reason": item.terminal_reason,
    }


def _decode_item(raw: object) -> TurnQueueItem:
    value = _mapping(raw, _ITEM_FIELDS, "turn queue item")
    identity_raw = _mapping(value["identity"], _IDENTITY_FIELDS, "turn queue identity")
    delivery_ids_raw = identity_raw["delivery_ids"]
    if type(delivery_ids_raw) is not list:
        raise ValueError("turn queue delivery identities are invalid")
    identity = TurnQueueIdentity(
        queue_id=_string(identity_raw["queue_id"], "queue_id"),
        request_id=_string(identity_raw["request_id"], "request_id"),
        root_id=_string(identity_raw["root_id"], "root_id"),
        subtree_id=_string(identity_raw["subtree_id"], "subtree_id"),
        agent_id=_string(identity_raw["agent_id"], "agent_id"),
        delivery_ids=tuple(_string(value, "delivery_id") for value in delivery_ids_raw),
    )
    priority_value = _integer(value["priority"], "priority", minimum=0)
    try:
        priority = TurnPriority(priority_value)
    except ValueError as exc:
        raise ValueError("turn queue priority is unsupported") from exc
    state_raw = _string(value["state"], "state")
    try:
        state = TurnQueueState(state_raw)
    except ValueError as exc:
        raise ValueError("turn queue state is unsupported") from exc
    settlement_raw = _optional_string(value["settlement_state"], "settlement_state")
    try:
        settlement_state = None if settlement_raw is None else TurnQueueState(settlement_raw)
    except ValueError as exc:
        raise ValueError("turn settlement state is unsupported") from exc
    return TurnQueueItem(
        identity=identity,
        enqueue_sequence=_integer(value["enqueue_sequence"], "enqueue_sequence", minimum=1),
        config_generation=_integer(value["config_generation"], "config_generation", minimum=1),
        revision=_integer(value["revision"], "revision", minimum=1),
        priority=priority,
        state=state,
        accepted_at=AbsoluteInstant.from_dict(value["accepted_at"]),
        deadline=_optional_instant(value["deadline"]),
        attempt=_integer(value["attempt"], "attempt", minimum=0),
        maximum_attempts=_integer(value["maximum_attempts"], "maximum_attempts", minimum=1),
        next_eligible_at=_optional_instant(value["next_eligible_at"]),
        claim=None if value["claim"] is None else _decode_claim(value["claim"]),
        payload_digest=_string(value["payload_digest"], "payload_digest"),
        settlement_state=settlement_state,
        terminal_reason=_optional_string(value["terminal_reason"], "terminal_reason"),
    )


def _encode_claim(claim: TurnClaimBinding) -> dict[str, object]:
    return {
        "scheduler_subject": claim.scheduler_subject,
        "scheduler_owner_id": claim.scheduler_owner_id,
        "scheduler_fencing_token": claim.scheduler_fencing_token,
        "process_instance_id": claim.process_instance_id,
        "execution_permit_receipt": claim.execution_permit_receipt.permit_id,
        "queue_revision": claim.queue_revision,
        "claimed_at": claim.claimed_at.to_dict(),
    }


def _decode_claim(raw: object) -> TurnClaimBinding:
    value = _mapping(raw, _CLAIM_FIELDS, "turn claim")
    return TurnClaimBinding(
        scheduler_subject=_string(value["scheduler_subject"], "scheduler_subject"),
        scheduler_owner_id=_string(value["scheduler_owner_id"], "scheduler_owner_id"),
        scheduler_fencing_token=_integer(value["scheduler_fencing_token"], "scheduler_fencing_token", minimum=1),
        process_instance_id=_string(value["process_instance_id"], "process_instance_id"),
        execution_permit_receipt=TurnCapacityPermitReceipt(
            _string(value["execution_permit_receipt"], "execution_permit_receipt")
        ),
        queue_revision=_integer(value["queue_revision"], "queue_revision", minimum=1),
        claimed_at=AbsoluteInstant.from_dict(value["claimed_at"]),
    )


def _mapping(raw: object, fields: set[str], label: str) -> JsonObject:
    if type(raw) is not dict or set(raw) != fields:
        raise ValueError(f"{label} fields are not canonical")
    return cast(JsonObject, raw)


def _string(raw: object, label: str) -> str:
    if type(raw) is not str or not raw:
        raise ValueError(f"turn queue {label} primitive is invalid")
    return raw


def _optional_string(raw: object, label: str) -> str | None:
    if raw is None:
        return None
    return _string(raw, label)


def _integer(raw: object, label: str, *, minimum: int) -> int:
    if type(raw) is not int or raw < minimum:
        raise ValueError(f"turn queue {label} primitive is invalid")
    return raw


def _optional_instant(raw: object) -> AbsoluteInstant | None:
    return None if raw is None else AbsoluteInstant.from_dict(raw)


__all__ = ["TURN_QUEUE_SCHEMA", "TurnQueueSnapshot", "decode_turn_queue", "encode_turn_queue"]
