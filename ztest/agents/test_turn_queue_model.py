from __future__ import annotations

import pytest

from mote.contracts.agent.capacity import TurnCapacityPermitReceipt
from mote.contracts.clock import UNIX_UTC_CLOCK, AbsoluteInstant
from mote.orchestration.agents.turn_queue import (
    TurnClaimBinding,
    TurnPriority,
    TurnQueueIdentity,
    TurnQueueItem,
    TurnQueueState,
)


def _instant(value: int) -> AbsoluteInstant:
    return AbsoluteInstant(1, UNIX_UTC_CLOCK, value)


def _identity() -> TurnQueueIdentity:
    return TurnQueueIdentity("queue-1", "request-1", "root-1", "subtree-1", "agent-1", ("delivery-1",))


def test_accepted_item_binds_mailbox_delivery_and_durable_order() -> None:
    item = TurnQueueItem(
        identity=_identity(),
        enqueue_sequence=7,
        config_generation=2,
        revision=1,
        priority=TurnPriority.NORMAL,
        state=TurnQueueState.ACCEPTED,
        accepted_at=_instant(10),
        deadline=_instant(20),
        attempt=0,
        maximum_attempts=3,
        next_eligible_at=None,
    )
    assert item.eligible
    assert item.identity.delivery_ids == ("delivery-1",)


def test_claim_requires_fence_process_permit_and_current_revision() -> None:
    claim = TurnClaimBinding(
        "turn-queue", "scheduler-1", 4, "process-1", TurnCapacityPermitReceipt("permit-9"), 2, _instant(12)
    )
    item = TurnQueueItem(
        identity=_identity(),
        enqueue_sequence=1,
        config_generation=1,
        revision=2,
        priority=TurnPriority.HIGH,
        state=TurnQueueState.CLAIMED,
        accepted_at=_instant(10),
        deadline=None,
        attempt=1,
        maximum_attempts=3,
        next_eligible_at=None,
        claim=claim,
    )
    assert item.claim is claim
    with pytest.raises(ValueError, match="current queue revision"):
        TurnQueueItem(
            identity=_identity(),
            enqueue_sequence=1,
            config_generation=1,
            revision=3,
            priority=TurnPriority.HIGH,
            state=TurnQueueState.CLAIMED,
            accepted_at=_instant(10),
            deadline=None,
            attempt=1,
            maximum_attempts=3,
            next_eligible_at=None,
            claim=claim,
        )


def test_terminal_and_retry_invariants_fail_closed() -> None:
    with pytest.raises(ValueError, match="terminal turn"):
        TurnQueueItem(
            identity=_identity(),
            enqueue_sequence=1,
            config_generation=1,
            revision=2,
            priority=TurnPriority.NORMAL,
            state=TurnQueueState.EXPIRED,
            accepted_at=_instant(10),
            deadline=_instant(20),
            attempt=0,
            maximum_attempts=2,
            next_eligible_at=None,
        )
    with pytest.raises(ValueError, match="bounded range"):
        TurnQueueItem(
            identity=_identity(),
            enqueue_sequence=1,
            config_generation=1,
            revision=1,
            priority=TurnPriority.NORMAL,
            state=TurnQueueState.ACCEPTED,
            accepted_at=_instant(10),
            deadline=None,
            attempt=3,
            maximum_attempts=2,
            next_eligible_at=None,
        )


def test_unknown_string_state_and_priority_are_rejected() -> None:
    with pytest.raises(TypeError, match="priority"):
        TurnQueueItem(
            identity=_identity(),
            enqueue_sequence=1,
            config_generation=1,
            revision=1,
            priority="high",  # type: ignore[arg-type]
            state=TurnQueueState.ACCEPTED,
            accepted_at=_instant(10),
            deadline=None,
            attempt=0,
            maximum_attempts=2,
            next_eligible_at=None,
        )
