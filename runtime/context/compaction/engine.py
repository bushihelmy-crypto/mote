"""L2 entry point — the ContextEngine that drives a reduction to completion.

The engine is what a caller (``ContextManager.manage_history`` for threshold
reductions, recovery for reactive ones) actually talks to. A sealed
CompactionPolicy decides the bounded profile and instructions before the
pipeline runs; successful reductions then emit observation facts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mote.contracts.policy.compaction import CompactionIntent
from mote.runtime.context.compaction.pipeline import ReductionPipeline
from mote.runtime.context.compaction.policy import build_compaction_policy
from mote.runtime.context.compaction.reducers.base import ReductionOutcome
from mote.runtime.context.compaction.request import ReductionRequest, Urgency
from mote.runtime.context.compaction.transcript import Transcript
from mote.runtime.events import ContextCompactedEvent, PostCompactEvent

if TYPE_CHECKING:
    from mote.contracts.ports import SessionFactSink


class ContextEngine:
    """Wraps a :class:`ReductionPipeline` with the compaction event lifecycle."""

    def __init__(
        self,
        pipeline: ReductionPipeline,
        *,
        telemetry=None,
        summarize_reducer=None,
        policy=None,
        session_fact_sink: "SessionFactSink | None" = None,
    ) -> None:
        self._pipeline = pipeline
        # Optional telemetry runtime for completed compaction observations.
        self._telemetry = telemetry
        # The summarize reducer, if any — the engine sets its per-pass
        # ``custom_instructions`` selected by the compaction policy
        # before running the pipeline.
        self._summarize = summarize_reducer
        self._policy = policy or build_compaction_policy()
        self._session_fact_sink = session_fact_sink

    def bind_telemetry(self, telemetry) -> None:
        self._telemetry = telemetry

    async def reduce(
        self,
        transcript: Transcript,
        request: ReductionRequest,
        *,
        trigger: str = "auto",
        custom_instructions: str | None = None,
    ) -> ReductionOutcome:
        """Apply policy, reduce toward the target, then publish completed facts."""
        source_message_ids = [str(message.id) for message in transcript.to_messages()]
        decision = await self._policy.process(
            CompactionIntent(
                trigger=trigger,
                target_tokens=request.target_tokens,
                urgency=request.urgency.value,
                custom_instructions=custom_instructions or "",
            )
        )
        if self._summarize is not None:
            self._summarize.custom_instructions = decision.custom_instructions or None
        if request.allow_destructive and not decision.allow_destructive:
            request = ReductionRequest(
                target_tokens=request.target_tokens,
                urgency=Urgency.SOFT,
                reason=request.reason,
            )

        outcome = await self._pipeline.run(transcript, request)

        # Every reduction changes only the model-context projection. The source
        # message facts remain independently replayable as the logical transcript.
        # Commit before returning the reduced view so callers cannot install a
        # projection that recovery cannot reproduce.
        if outcome.changed:
            rebuilt = outcome.transcript.to_messages()
            event = ContextCompactedEvent(
                model_context_messages=list(rebuilt),
                source_message_ids=source_message_ids,
                summary=outcome.summary or "",
                strategy=outcome.strategy,
                trigger=trigger,
            )
            if self._session_fact_sink is not None:
                await self._session_fact_sink.commit_fact(event)
            if self._telemetry is not None:
                await self._telemetry.emit(event)
                await self._telemetry.emit(PostCompactEvent(trigger=trigger, summary=outcome.summary or ""))

        return outcome
