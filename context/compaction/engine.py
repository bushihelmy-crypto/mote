"""L2 entry point — the ContextEngine that drives a reduction to completion.

The engine is what a caller (``ContextManager.manage_history`` for threshold
reductions, recovery for reactive ones) actually talks to. It owns the event
choreography around a reduction — PreCompact (veto / custom-instructions), and,
on a successful summarize, CompactionCheckpoint + PostCompact — so that logic
lives in exactly one place regardless of who raised the request. The pipeline underneath decides *how* to reduce; the engine wraps
it with *when to announce it*.
"""

from __future__ import annotations

from mote.common.events import CompactionCheckpointEvent, PostCompactEvent, PreCompactEvent
from mote.context.compaction.pipeline import ReductionPipeline
from mote.context.compaction.reducers.base import ReductionOutcome
from mote.context.compaction.request import ReductionRequest
from mote.context.compaction.transcript import Transcript


class ContextEngine:
    """Wraps a :class:`ReductionPipeline` with the compaction event lifecycle."""

    def __init__(self, pipeline: ReductionPipeline, *, bus=None, summarize_reducer=None) -> None:
        self._pipeline = pipeline
        # Optional event bus (``common.events.EventBus``). When set, a reduction
        # emits PreCompact (veto / instruction supply) up front and, on a
        # successful summarize, CompactionCheckpoint + PostCompact.
        self._bus = bus
        # The summarize reducer, if any — the engine sets its per-pass
        # ``custom_instructions`` (from the caller and/or a PreCompact hook)
        # before running the pipeline.
        self._summarize = summarize_reducer

    async def reduce(
        self,
        transcript: Transcript,
        request: ReductionRequest,
        *,
        trigger: str = "auto",
        custom_instructions: str | None = None,
    ) -> ReductionOutcome:
        """Reduce ``transcript`` toward ``request.target_tokens``.

        Emits PreCompact first (a subscriber may veto the whole pass or supply
        summarize ``custom_instructions``), runs the pipeline, then — only when a
        summarize actually happened — emits the checkpoint + PostCompact events.
        """
        # Seed the summarize reducer's per-pass instructions from the caller.
        if self._summarize is not None:
            self._summarize.custom_instructions = custom_instructions

        if self._bus is not None:
            pre = await self._bus.emit(PreCompactEvent(trigger=trigger))
            # ``None`` when no hook layer maps PreCompact (nothing to veto/supply).
            if pre is not None:
                if pre.cancel:
                    return ReductionOutcome(transcript, strategy="", target_met=False)
                if pre.additional_context and self._summarize is not None:
                    self._summarize.custom_instructions = "\n".join(pre.additional_context)

        outcome = await self._pipeline.run(transcript, request)

        # A summary set means the (LLM) summarize reducer rebuilt the history:
        # persist it as a replay checkpoint and announce the compaction.
        if outcome.summary and self._bus is not None:
            rebuilt = outcome.transcript.to_messages()
            await self._bus.emit(CompactionCheckpointEvent(messages=list(rebuilt), summary=outcome.summary))
            await self._bus.emit(PostCompactEvent(trigger=trigger, summary=outcome.summary))

        return outcome
