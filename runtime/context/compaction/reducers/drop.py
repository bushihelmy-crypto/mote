"""HeadDropReducer (DESTRUCTIVE) — irreversibly drop the oldest turns.

The last-resort strategy. When even a summarize could not (or must not) run and
the transcript still overflows the window, drop the oldest non-pinned segments —
whole segments only, so the tool_call↔tool_result pairing is never broken — and
prepend a short ``[earlier turns truncated]`` marker so the model knows history
was cut.

This is the only lossy reducer: dropped turns are gone (no summary, no fold).
It therefore runs *only* under ``HARD`` urgency (``request.allow_destructive``)
and only after the cheaper reducers already ran and left us over target — the
pipeline enforces that ordering. Pinned segments (system anchors / task) are
never dropped, and at least the most-recent non-pinned segment is always kept.
"""

from __future__ import annotations

from mote.contracts.conversation import ContextManagerConfig, Message, UserMessage
from mote.contracts.conversation.constants import HEAD_DROPPED_MESSAGE
from mote.runtime.context.compaction.reducers.base import ReducerCost, ReductionOutcome
from mote.runtime.context.compaction.request import ReductionRequest
from mote.runtime.context.compaction.transcript import Transcript


class HeadDropReducer:
    """DESTRUCTIVE strategy: drop the oldest non-pinned segments to fit target."""

    cost = ReducerCost.DESTRUCTIVE

    def __init__(self, config: ContextManagerConfig | None = None, *, model: str = "gpt-4") -> None:
        self._cfg = config or ContextManagerConfig()
        self._model = model

    async def reduce(self, transcript: Transcript, request: ReductionRequest) -> ReductionOutcome:
        model = self._model
        # Lossy: only ever runs when the request explicitly allows it (HARD).
        if not request.allow_destructive:
            return ReductionOutcome(transcript, strategy="head_drop")

        pre = transcript.token_count(model)
        if pre <= request.target_tokens:
            return ReductionOutcome(transcript, strategy="head_drop", target_met=True)

        pinned = [s for s in transcript.segments if s.pinned]
        non_pinned = [s for s in transcript.segments if not s.pinned]
        # Nothing droppable while still keeping one recent segment.
        if len(non_pinned) <= 1:
            return ReductionOutcome(transcript, strategy="head_drop")

        pinned_msgs = Transcript(pinned).to_messages()
        marker = UserMessage(content=HEAD_DROPPED_MESSAGE)

        # Drop oldest-first, one segment at a time, until the rebuilt transcript
        # fits — always leaving at least the most-recent non-pinned segment.
        new_transcript = transcript
        for drop_count in range(1, len(non_pinned)):
            kept_msgs: list[Message] = []
            for s in non_pinned[drop_count:]:
                kept_msgs.extend(s.messages)
            new_transcript = Transcript.from_messages([*pinned_msgs, marker, *kept_msgs])
            if new_transcript.token_count(model) <= request.target_tokens:
                break

        post = new_transcript.token_count(model)
        return ReductionOutcome(
            new_transcript,
            tokens_freed=max(0, pre - post),
            changed=True,
            strategy="head_drop",
            target_met=post <= request.target_tokens,
        )
