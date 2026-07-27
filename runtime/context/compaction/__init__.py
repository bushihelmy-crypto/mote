"""Context compaction — the unified reduction pipeline.

Three orthogonal concerns, cleanly separated:

- **L0 ``transcript``** — what the conversation *is*: a segmentation that makes
  the tool_call↔tool_result pairing atomic, so a cut can never break it.
- **L1 ``request``** — *when/how much* to reduce: a :class:`ReductionRequest`
  (target tokens + urgency + reason) that unifies threshold-triggered and
  reactive (context-overflow) reductions behind one entry point.
- **L2 ``reducers`` + ``pipeline`` + ``engine``** — *how* to reduce: pluggable
  strategies (fold → summarize → drop) run cheapest-first until the target is
  met.
"""

from __future__ import annotations

from mote.runtime.context.compaction.engine import ContextEngine
from mote.runtime.context.compaction.pipeline import ReductionPipeline
from mote.runtime.context.compaction.recovery import RecoveryContextReducer
from mote.runtime.context.compaction.reducers import (
    EraseReducer,
    FoldReducer,
    HeadDropReducer,
    OversizedSpillReducer,
    Reducer,
    ReducerCost,
    ReductionOutcome,
    SummarizeReducer,
)
from mote.runtime.context.compaction.rehydrate import FileRehydrator
from mote.runtime.context.compaction.request import ReductionReason, ReductionRequest, Urgency
from mote.runtime.context.compaction.transcript import PINNED_KINDS, Segment, SegmentKind, Transcript

__all__ = [
    "Transcript",
    "Segment",
    "SegmentKind",
    "PINNED_KINDS",
    "ReductionRequest",
    "Urgency",
    "ReductionReason",
    "Reducer",
    "ReducerCost",
    "ReductionOutcome",
    "EraseReducer",
    "FoldReducer",
    "OversizedSpillReducer",
    "SummarizeReducer",
    "HeadDropReducer",
    "ReductionPipeline",
    "ContextEngine",
    "RecoveryContextReducer",
    "FileRehydrator",
]
