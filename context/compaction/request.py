"""L1 — a single, uniform way to *ask* for a reduction.

Before this, two independent code paths decided when and how much to compact:
the threshold-triggered ``manage_history`` and the context-overflow ``_compress``
inside ``base_llm``. They each hard-coded their own decision and mechanism, so a
change to one silently diverged from the other.

A :class:`ReductionRequest` unifies both behind one description of *intent*:

- ``target_tokens`` — reduce until at or below this size.
- ``urgency`` — ``SOFT`` (proactive, threshold-driven: stop before destructive
  strategies) vs ``HARD`` (reactive, we already hit the wall: destructive
  head-drop is allowed as a last resort).
- ``reason`` — why the request was raised (for tracing / future policy).

The engine + pipeline consume this request; the caller does not pick a
mechanism, it only states the goal.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Urgency(Enum):
    """How hard the pipeline is allowed to push."""

    # Proactive: fired on crossing a threshold with headroom to spare. The
    # pipeline stops before any destructive (lossy, non-reconstructable) strategy.
    SOFT = "soft"
    # Reactive: the request already overflowed the window. The pipeline may
    # escalate to destructive head-drop to make the call fit at all.
    HARD = "hard"


class ReductionReason(Enum):
    """Why a reduction was requested (advisory; drives tracing, not control flow)."""

    # Crossed the autocompact token threshold during normal turn management.
    THRESHOLD = "threshold"
    # A live call overflowed the context window and is being recovered.
    REACTIVE = "reactive"
    # --- Reserved for future scopes (not raised this round) ---
    MID_TURN = "mid_turn"
    DOWNSHIFT = "downshift"
    COMP_HASH = "comp_hash"


@dataclass(frozen=True)
class ReductionRequest:
    """A request to reduce a transcript to at most ``target_tokens``."""

    target_tokens: int
    urgency: Urgency = Urgency.SOFT
    reason: ReductionReason = ReductionReason.THRESHOLD

    @property
    def allow_destructive(self) -> bool:
        """Destructive (lossy) reducers may run only under HARD urgency."""
        return self.urgency is Urgency.HARD
