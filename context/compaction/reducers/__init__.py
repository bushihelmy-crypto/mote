"""L2 reducers — the pluggable context-reduction strategies.

Each reducer implements the :class:`Reducer` protocol (``cost`` + ``async
reduce``) and is ordered by :class:`ReducerCost`. The pipeline runs them
cheapest-first and stops once the target is met:

- :class:`EraseReducer` (FREE) — pair-delete results the model marked erasable.
- :class:`FoldReducer` (FREE) — clear old reconstructable tool-result bodies.
- :class:`SummarizeReducer` (LLM) — summarize the head, keep a verbatim tail.
- :class:`HeadDropReducer` (DESTRUCTIVE) — drop the oldest turns as a last resort.
"""

from __future__ import annotations

from metagpt.context.compaction.reducers.base import (
    Reducer,
    ReducerCost,
    ReductionOutcome,
)
from metagpt.context.compaction.reducers.drop import HeadDropReducer
from metagpt.context.compaction.reducers.erase import EraseReducer
from metagpt.context.compaction.reducers.fold import FoldReducer
from metagpt.context.compaction.reducers.summarize import SummarizeReducer

__all__ = [
    "Reducer",
    "ReducerCost",
    "ReductionOutcome",
    "EraseReducer",
    "FoldReducer",
    "SummarizeReducer",
    "HeadDropReducer",
]
