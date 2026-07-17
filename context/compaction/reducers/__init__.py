"""L2 reducers — the pluggable context-reduction strategies.

Each reducer implements the :class:`Reducer` protocol (``cost`` + ``async
reduce``) and is ordered by :class:`ReducerCost`. The pipeline runs them
cheapest-first and stops once the target is met:

- :class:`EraseReducer` (FREE) — pair-delete results the model marked erasable.
- :class:`FoldReducer` (FREE) — clear old reconstructable tool-result bodies.
- :class:`OversizedSpillReducer` (FREE) — spill runaway single parts (message
  content / tool-call args) to disk, leaving a ``<persisted-output>`` pointer.
- :class:`SummarizeReducer` (LLM) — summarize the head, keep a verbatim tail.
- :class:`HeadDropReducer` (DESTRUCTIVE) — drop the oldest turns as a last resort.
"""

from __future__ import annotations

from mote.context.compaction.reducers.base import Reducer, ReducerCost, ReductionOutcome
from mote.context.compaction.reducers.drop import HeadDropReducer
from mote.context.compaction.reducers.erase import EraseReducer
from mote.context.compaction.reducers.fold import FoldReducer
from mote.context.compaction.reducers.spill import OversizedSpillReducer
from mote.context.compaction.reducers.summarize import SummarizeReducer

__all__ = [
    "Reducer",
    "ReducerCost",
    "ReductionOutcome",
    "EraseReducer",
    "FoldReducer",
    "OversizedSpillReducer",
    "SummarizeReducer",
    "HeadDropReducer",
]
