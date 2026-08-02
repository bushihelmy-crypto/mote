from __future__ import annotations

from dataclasses import replace

from mote.contracts.clock import UNIX_UTC_CLOCK, AbsoluteInstant
from mote.orchestration.agents.turn_queue.codec import TurnQueueSnapshot
from mote.orchestration.agents.turn_queue.model import TurnPriority, TurnQueueIdentity, TurnQueueItem, TurnQueueState
from mote.orchestration.agents.turn_queue.scheduling import RootTurnWeight, TurnSchedulingConfig, choose_turn


def _instant(value: int) -> AbsoluteInstant:
    return AbsoluteInstant(1, UNIX_UTC_CLOCK, value)


def _item(
    sequence: int,
    root: str,
    subtree: str,
    *,
    priority: TurnPriority = TurnPriority.NORMAL,
    eligible_at: int | None = None,
) -> TurnQueueItem:
    return TurnQueueItem(
        identity=TurnQueueIdentity(
            "queue-1", f"request-{sequence}", root, subtree, f"agent-{sequence}", (f"delivery-{sequence}",)
        ),
        enqueue_sequence=sequence,
        config_generation=1,
        revision=1,
        priority=priority,
        state=TurnQueueState.ACCEPTED,
        accepted_at=_instant(1),
        deadline=_instant(100),
        attempt=0,
        maximum_attempts=3,
        next_eligible_at=None if eligible_at is None else _instant(eligible_at),
    )


def _claims(items: tuple[TurnQueueItem, ...], config: TurnSchedulingConfig, count: int) -> tuple[str, ...]:
    snapshot = TurnQueueSnapshot("queue-1", 1, len(items) + 1, 64, items)
    roots: list[str] = []
    for _ in range(count):
        decision = choose_turn(snapshot, config=config, now=_instant(10))
        assert decision is not None
        roots.append(decision.item.identity.root_id)
        remaining = tuple(item for item in snapshot.items if item is not decision.item)
        snapshot = replace(snapshot, items=remaining, scheduling=decision.scheduling)
    return tuple(roots)


def test_root_weight_changes_share_without_starving_low_weight_root() -> None:
    items = tuple(_item(index, "root-a", "a") for index in range(1, 7)) + tuple(
        _item(index, "root-b", "b") for index in range(7, 10)
    )
    config = TurnSchedulingConfig(1, (RootTurnWeight("root-a", 3), RootTurnWeight("root-b", 1)))
    assert _claims(items, config, 8)[:4] == ("root-a", "root-a", "root-a", "root-b")


def test_priority_flood_cannot_cross_root_fairness() -> None:
    items = tuple(_item(index, "root-a", "a", priority=TurnPriority.URGENT) for index in range(1, 5)) + tuple(
        _item(index, "root-b", "b", priority=TurnPriority.LOW) for index in range(5, 8)
    )
    assert _claims(items, TurnSchedulingConfig(1), 4) == (
        "root-a",
        "root-b",
        "root-a",
        "root-b",
    )


def test_sibling_subtrees_are_fair_then_priority_and_fifo_apply_locally() -> None:
    items = (
        _item(1, "root-a", "left", priority=TurnPriority.LOW),
        _item(2, "root-a", "left", priority=TurnPriority.URGENT),
        _item(3, "root-a", "right", priority=TurnPriority.NORMAL),
        _item(4, "root-a", "right", priority=TurnPriority.NORMAL),
    )
    snapshot = TurnQueueSnapshot("queue-1", 1, 5, 8, items)
    selected: list[int] = []
    for _ in range(4):
        decision = choose_turn(snapshot, config=TurnSchedulingConfig(1), now=_instant(10))
        assert decision is not None
        selected.append(decision.item.enqueue_sequence)
        snapshot = replace(
            snapshot,
            items=tuple(item for item in snapshot.items if item is not decision.item),
            scheduling=decision.scheduling,
        )
    assert selected == [2, 3, 1, 4]


def test_future_retry_and_expired_poison_do_not_block_later_item() -> None:
    snapshot = TurnQueueSnapshot(
        "queue-1",
        1,
        4,
        8,
        (
            _item(1, "root-a", "left", eligible_at=50),
            replace(_item(2, "root-a", "left"), deadline=_instant(5)),
            _item(3, "root-a", "left"),
        ),
    )
    decision = choose_turn(snapshot, config=TurnSchedulingConfig(2), now=_instant(10))
    assert decision is not None
    assert decision.item.enqueue_sequence == 3
    assert decision.scheduling.config_generation == 2


def test_config_generation_update_preserves_existing_deficit_history() -> None:
    items = tuple(_item(index, "root-a", "a") for index in range(1, 5)) + (_item(5, "root-b", "b"),)
    snapshot = TurnQueueSnapshot("queue-1", 1, 6, 8, items)
    first = choose_turn(
        snapshot,
        config=TurnSchedulingConfig(1, (RootTurnWeight("root-a", 3),)),
        now=_instant(10),
    )
    assert first is not None
    snapshot = replace(
        snapshot,
        items=tuple(item for item in snapshot.items if item is not first.item),
        scheduling=first.scheduling,
    )
    after_update = choose_turn(snapshot, config=TurnSchedulingConfig(2), now=_instant(10))
    assert after_update is not None
    assert after_update.item.identity.root_id == "root-a"
    assert after_update.scheduling.config_generation == 2
