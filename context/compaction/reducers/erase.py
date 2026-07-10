"""EraseReducer (FREE) — true pair-deletion of results the model marked erasable.

The counterpart to fold. Where fold *keeps* a reconstructable tool_result but
shrinks its body to a placeholder (pairing intact, still re-derivable), erase
removes the result outright — together with its ``tool_call`` entry in the
invoking assistant turn, so the pairing stays legal (no orphan ``tool_result`` →
no Anthropic 400). It only touches results the producer explicitly tagged
``RETENTION_ERASABLE`` on the tool_result message: a per-result statement that
"the model has read this and no longer needs it," so dropping it loses nothing
the conversation still depends on. Results tagged ``RETENTION_PIN`` carry a
different value and are therefore never collected here.

Like fold this is FREE (no LLM round-trip), but it is *pressure-gated*: erasing
rewrites the request prefix and busts the prompt cache, so it fires only when the
transcript is actually over ``request.target_tokens`` — never trade a cache miss
for a trim we do not need ("压力未到就不擦"). Reconstructability is irrelevant to
erase: the tag, not the tool's re-derivability, is the authority for deletion.
"""

from __future__ import annotations

from metagpt.common.const import RETENTION, RETENTION_ERASABLE, TOOL_CALL_ID
from metagpt.common.schema import ContextManagerConfig, Message
from metagpt.common.utils.token_counter import count_string_tokens
from metagpt.context.compaction.reducers.base import ReducerCost, ReductionOutcome
from metagpt.context.compaction.request import ReductionRequest
from metagpt.context.compaction.transcript import SegmentKind, Transcript


class EraseReducer:
    """FREE strategy: pressure-gated pair-deletion of RETENTION_ERASABLE results."""

    cost = ReducerCost.FREE

    def __init__(self, config: ContextManagerConfig | None = None, *, model: str = "gpt-4") -> None:
        self._cfg = config or ContextManagerConfig()
        self._model = model

    async def reduce(self, transcript: Transcript, request: ReductionRequest) -> ReductionOutcome:
        model = self._model
        # Pressure gate: only pay the cache-busting cost of rewriting the prefix
        # when we are actually over target. Under target, leave the cache warm.
        if transcript.token_count(model) <= request.target_tokens:
            return ReductionOutcome(transcript, strategy="erase")

        erasable: list[Message] = []
        for seg in transcript.segments:
            if seg.kind is not SegmentKind.TOOL_GROUP:
                continue
            for msg in seg.messages:
                if not msg.metadata.get(TOOL_CALL_ID):
                    continue
                if msg.metadata.get(RETENTION) == RETENTION_ERASABLE:
                    erasable.append(msg)

        if not erasable:
            return ReductionOutcome(transcript, strategy="erase")

        call_ids = [m.metadata.get(TOOL_CALL_ID) for m in erasable]
        tokens_freed = sum(count_string_tokens(m.content or "", model) for m in erasable)
        new_transcript = transcript.erase_pairs(call_ids)
        target_met = new_transcript.token_count(model) <= request.target_tokens
        return ReductionOutcome(
            new_transcript,
            tokens_freed=tokens_freed,
            changed=True,
            strategy="erase",
            target_met=target_met,
        )
