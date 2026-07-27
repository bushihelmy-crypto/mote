"""L2 orchestration — the cheapest-first reduction pipeline.

The pipeline is the one place that knows the *order* strategies run in, and it
derives that order from the reducers themselves (their :class:`ReducerCost`), so
adding a fourth strategy never means editing an orchestrator — you just hand the
pipeline one more reducer.

The run policy encodes the plan's three rules:

- **FREE reducers always run** (opportunistically). Fold is count-gated
  internally; it tidies reconstructable bodies whether or not we are over target.
- **Costly reducers (LLM / DESTRUCTIVE) run only while still over target.** Once
  a reducer brings the transcript at/under ``request.target_tokens`` the pipeline
  stops — it never spends an LLM call or drops turns it does not need to.
- **DESTRUCTIVE runs only under HARD urgency** (``request.allow_destructive``).
  Under SOFT the pipeline stops before any lossy strategy.
"""

from __future__ import annotations

from typing import Iterable

from mote.runtime.context.compaction.reducers.base import Reducer, ReducerCost, ReductionOutcome
from mote.runtime.context.compaction.request import ReductionRequest
from mote.runtime.context.compaction.transcript import Transcript


class ReductionPipeline:
    """Runs reducers cheapest-first, stopping as soon as the target is met."""

    def __init__(self, reducers: Iterable[Reducer], *, model: str = "gpt-4") -> None:
        # Sort ascending by cost so FREE runs before LLM before DESTRUCTIVE.
        self._reducers: list[Reducer] = sorted(reducers, key=lambda r: int(r.cost))
        self._model = model

    async def run(self, transcript: Transcript, request: ReductionRequest) -> ReductionOutcome:
        current = transcript
        changed = False
        total_freed = 0
        strategy = ""
        summary: str | None = None

        for reducer in self._reducers:
            is_free = reducer.cost <= ReducerCost.FREE
            if not is_free:
                # Costly strategies: only while still over target, and never a
                # destructive one under SOFT urgency.
                if current.token_count(self._model) <= request.target_tokens:
                    break
                if reducer.cost >= ReducerCost.DESTRUCTIVE and not request.allow_destructive:
                    break

            outcome = await reducer.reduce(current, request)
            if outcome.changed:
                current = outcome.transcript
                changed = True
                total_freed += outcome.tokens_freed
                strategy = outcome.strategy
                if outcome.summary:
                    summary = outcome.summary

        target_met = current.token_count(self._model) <= request.target_tokens
        return ReductionOutcome(
            current,
            tokens_freed=total_freed,
            changed=changed,
            strategy=strategy,
            target_met=target_met,
            summary=summary,
        )
