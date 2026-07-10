"""L2 base — the reducer contract every compaction strategy implements.

A reducer is a pluggable zero-config strategy: given a :class:`Transcript` and a
:class:`ReductionRequest`, it returns a :class:`ReductionOutcome` describing what
(if anything) it did. The :class:`ReductionPipeline` runs reducers cheapest-first
and stops as soon as the target is met — so adding a fourth strategy is a matter
of writing one more reducer, never editing an orchestrator.

Cost tiers order the pipeline and gate escalation:

- ``FREE`` — no LLM, no information loss beyond re-derivable tool output (fold).
  Always runs opportunistically.
- ``LLM`` — one summarization call (summarize). Runs only when still over target.
- ``DESTRUCTIVE`` — irreversibly drops old turns (head-drop). Runs only under a
  HARD request, and only when everything cheaper still left us over target.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Protocol, runtime_checkable

from metagpt.context.compaction.request import ReductionRequest
from metagpt.context.compaction.transcript import Transcript


class ReducerCost(IntEnum):
    """Relative cost/aggression of a reducer — the pipeline sorts ascending."""

    FREE = 0
    LLM = 1
    DESTRUCTIVE = 2


@dataclass
class ReductionOutcome:
    """What a reducer did to a transcript.

    ``transcript`` is the (possibly rebuilt) transcript to carry forward — the
    original when nothing changed. ``changed`` is True iff this reducer altered
    the history. ``target_met`` reports whether the transcript is now at or below
    the request's ``target_tokens`` (the pipeline's stop condition).
    """

    transcript: Transcript
    tokens_freed: int = 0
    changed: bool = False
    strategy: str = ""
    target_met: bool = False
    # Set by the summarize reducer on a successful summarization so the engine can
    # emit the compaction checkpoint / post-compact events. None otherwise.
    summary: str | None = None


@runtime_checkable
class Reducer(Protocol):
    """A pluggable context-reduction strategy."""

    cost: ReducerCost

    async def reduce(self, transcript: Transcript, request: ReductionRequest) -> ReductionOutcome:
        """Attempt to shrink ``transcript`` toward ``request.target_tokens``."""
        ...
