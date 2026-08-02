"""Session-owned policy for observation facts eligible for rollout persistence."""

from __future__ import annotations

from typing import get_args

from mote.contracts.ports.session.facts import RolloutSourceEvent

# Persistence is a session policy, orthogonal to whether CLI/logging/telemetry
# also consume the same fact.
ROLLOUT_EVENT_TYPES: frozenset[type[RolloutSourceEvent]] = frozenset(get_args(RolloutSourceEvent))


def is_rollout_event(event: object) -> bool:
    """Whether the recorder owns a persistence projection for ``event``."""
    return type(event) in ROLLOUT_EVENT_TYPES


__all__ = ["ROLLOUT_EVENT_TYPES", "is_rollout_event"]
