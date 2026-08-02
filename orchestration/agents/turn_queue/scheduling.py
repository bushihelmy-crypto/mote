"""Owner-neutral deterministic WDRR policy for durable Agent turns."""

from __future__ import annotations

from dataclasses import dataclass

from mote.contracts.clock import AbsoluteInstant
from mote.orchestration.agents.turn_queue.codec import TurnQueueSnapshot
from mote.orchestration.agents.turn_queue.model import (
    TurnQueueItem,
    TurnSchedulingCursor,
    TurnSchedulingDeficit,
    TurnSchedulingState,
    TurnSubtreeCursor,
)

MAX_ROOT_WEIGHT = 64


@dataclass(frozen=True, slots=True)
class RootTurnWeight:
    root_id: str
    units: int

    def __post_init__(self) -> None:
        if type(self.root_id) is not str or not self.root_id:
            raise ValueError("turn root weight identity is invalid")
        if type(self.units) is not int or not 1 <= self.units <= MAX_ROOT_WEIGHT:
            raise ValueError("turn root weight is outside the Product bound")


@dataclass(frozen=True, slots=True)
class TurnSchedulingConfig:
    generation: int
    root_weights: tuple[RootTurnWeight, ...] = ()

    def __post_init__(self) -> None:
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("turn scheduling config generation is invalid")
        if not isinstance(self.root_weights, tuple) or not all(
            isinstance(value, RootTurnWeight) for value in self.root_weights
        ):
            raise TypeError("turn root weights must be a typed tuple")
        roots = tuple(value.root_id for value in self.root_weights)
        if len(roots) != len(set(roots)):
            raise ValueError("turn root weights must be unique")

    def weight_for(self, root_id: str) -> int:
        return next((value.units for value in self.root_weights if value.root_id == root_id), 1)


@dataclass(frozen=True, slots=True)
class TurnSchedulingDecision:
    item: TurnQueueItem
    scheduling: TurnSchedulingState


def choose_turn(
    snapshot: TurnQueueSnapshot,
    *,
    config: TurnSchedulingConfig,
    now: AbsoluteInstant,
) -> TurnSchedulingDecision | None:
    eligible = tuple(item for item in snapshot.items if _is_eligible(item, now))
    if not eligible:
        return None
    deficits = {(value.root_id, value.subtree_id): value.units for value in snapshot.scheduling.deficits}
    root_order = _ordered_identities(eligible, root=True)
    root_id, next_root = _weighted_pick(
        root_order,
        snapshot.scheduling.cursor.root_id,
        deficits,
        key_prefix=None,
        weights={root: config.weight_for(root) for root in root_order},
    )
    root_items = tuple(item for item in eligible if item.identity.root_id == root_id)
    subtree_order = _ordered_identities(root_items, root=False)
    subtree_cursor_by_root = {value.root_id: value.subtree_id for value in snapshot.scheduling.cursor.subtrees}
    subtree_id, next_subtree = _weighted_pick(
        subtree_order,
        subtree_cursor_by_root.get(root_id),
        deficits,
        key_prefix=root_id,
        weights={subtree: 1 for subtree in subtree_order},
    )
    candidates = tuple(item for item in root_items if item.identity.subtree_id == subtree_id)
    selected = min(candidates, key=lambda item: (-int(item.priority), item.enqueue_sequence))
    subtree_cursor_by_root[root_id] = next_subtree
    state = TurnSchedulingState(
        config_generation=config.generation,
        cursor=TurnSchedulingCursor(
            next_root,
            tuple(
                TurnSubtreeCursor(cursor_root, cursor_subtree)
                for cursor_root, cursor_subtree in sorted(subtree_cursor_by_root.items())
            ),
        ),
        deficits=tuple(
            TurnSchedulingDeficit(deficit_root, deficit_subtree, units)
            for (deficit_root, deficit_subtree), units in sorted(
                deficits.items(), key=lambda value: (value[0][0], value[0][1] or "")
            )
        ),
    )
    return TurnSchedulingDecision(selected, state)


def _is_eligible(item: TurnQueueItem, now: AbsoluteInstant) -> bool:
    if not item.eligible:
        return False
    now.require_clock(item.accepted_at.clock)
    if item.deadline is not None and now.is_at_or_after(item.deadline):
        return False
    return item.next_eligible_at is None or now.is_at_or_after(item.next_eligible_at)


def _ordered_identities(items: tuple[TurnQueueItem, ...], *, root: bool) -> tuple[str, ...]:
    first_sequence: dict[str, int] = {}
    for item in items:
        identity = item.identity.root_id if root else item.identity.subtree_id
        first_sequence.setdefault(identity, item.enqueue_sequence)
    return tuple(sorted(first_sequence, key=lambda identity: first_sequence[identity]))


def _weighted_pick(
    order: tuple[str, ...],
    cursor: str | None,
    deficits: dict[tuple[str, str | None], int],
    *,
    key_prefix: str | None,
    weights: dict[str, int],
) -> tuple[str, str]:
    index = order.index(cursor) if cursor in order else 0
    identity = order[index]
    key = (identity, None) if key_prefix is None else (key_prefix, identity)
    units = deficits.get(key, 0)
    if units == 0:
        units = weights[identity]
    units -= 1
    deficits[key] = units
    next_cursor = identity if units > 0 else order[(index + 1) % len(order)]
    return identity, next_cursor


__all__ = [
    "MAX_ROOT_WEIGHT",
    "RootTurnWeight",
    "TurnSchedulingConfig",
    "TurnSchedulingDecision",
    "choose_turn",
]
