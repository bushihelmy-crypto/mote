"""FoldReducer (FREE) — clear old reconstructable tool-result bodies in place.

The cheap, no-LLM strategy (the former ``microcompact``): once enough
reconstructable tool results have piled up, replace the *content* of all but the
most-recent N with a short placeholder. The tool_call↔tool_result pairing is left
fully intact — only the text shrinks. Keeping the pairing intact preserves
request *legality* (no orphan ``tool_result`` → no Anthropic 400); it does **not**
preserve the prompt cache. Anthropic caching is a strict prefix match, so
rewriting an old message's content changes the prefix and invalidates the cache
from that point — a one-time cache-write cost. That cost only pays off once
amortized over later turns (the folded prefix restabilizes and cache hits
resume), which is why folding is gated on freeing at least
``microcompact_clear_at_least`` tokens (mirroring Anthropic context-editing's
``clear_at_least``): never eat a cache miss for a trivial trim.

"Reconstructable" is decided **once**, upstream, by :meth:`Transcript.from_messages`:
a ``TOOL_GROUP`` is stamped ``reconstructable`` when every tool it invoked
self-declared itself so (each tool's ``reconstructable`` ClassVar; the Role
derives the set from the live executor and threads it into ``from_messages``).
This reducer simply *consumes* that segment flag — it never re-derives the
judgment. Conversational results (AskUserQuestion) and sticky resource bodies
(re-projected capability bodies) are never touched.

This is the same count-gated fold the old ``microcompact`` did, now expressed as
a pluggable reducer. It mutates ``Message.content`` in place (the point: shrink
what is kept) and reports the freed-token count.
"""

from __future__ import annotations

from metagpt.common.const import RESOURCE_STICKY, RETENTION, RETENTION_PIN, TOOL_CALL_ID
from metagpt.common.const.context import TOOL_RESULT_CLEARED_MESSAGE
from metagpt.common.schema import ContextManagerConfig, Message
from metagpt.common.utils.token_counter import count_string_tokens
from metagpt.context.compaction.reducers.base import ReducerCost, ReductionOutcome
from metagpt.context.compaction.request import ReductionRequest
from metagpt.context.compaction.transcript import SegmentKind, Transcript


class FoldReducer:
    """FREE strategy: count-gated in-place fold of old reconstructable results."""

    cost = ReducerCost.FREE

    def __init__(
        self,
        config: ContextManagerConfig | None = None,
        *,
        model: str = "gpt-4",
    ) -> None:
        self._cfg = config or ContextManagerConfig()
        self._model = model

    async def reduce(self, transcript: Transcript, request: ReductionRequest) -> ReductionOutcome:
        cfg = self._cfg
        model = self._model
        if not cfg.enable_microcompact:
            return ReductionOutcome(transcript, strategy="fold")

        keep_recent = max(1, cfg.microcompact_keep_recent)
        trigger = cfg.microcompact_trigger_threshold
        placeholder = TOOL_RESULT_CLEARED_MESSAGE

        # Reconstructable tool-result messages still holding real content, in
        # order. The single source of truth for "reconstructable" is the segment
        # flag stamped by ``Transcript.from_messages``; we consume it, never
        # re-compute. Sticky resource bodies (re-projected loaded capabilities)
        # are never folded — they are the very content the registry preserves.
        active: list[Message] = []
        for seg in transcript.segments:
            if seg.kind is not SegmentKind.TOOL_GROUP or not seg.reconstructable:
                continue
            for msg in seg.messages:
                if not msg.metadata.get(TOOL_CALL_ID):
                    continue
                if msg.content == placeholder or msg.metadata.get(RESOURCE_STICKY):
                    continue
                # A result the producer pinned (RETENTION_PIN) is never folded —
                # the model asked to keep this body verbatim (mirrors the sticky
                # skip above; the transcript also treats such a group as pinned
                # against summarize/drop).
                if msg.metadata.get(RETENTION) == RETENTION_PIN:
                    continue
                active.append(msg)

        if len(active) <= trigger:
            return ReductionOutcome(transcript, strategy="fold")

        to_clear = active[: len(active) - keep_recent]
        if not to_clear:
            return ReductionOutcome(transcript, strategy="fold")

        # Folding rewrites these bodies, changing the request prefix and forcing a
        # one-time prompt-cache write. Only worth it if it frees enough to amortize
        # that cost over later turns — otherwise skip and leave the cache warm.
        tokens_freed = sum(count_string_tokens(msg.content, model) for msg in to_clear)
        if tokens_freed < cfg.microcompact_clear_at_least:
            return ReductionOutcome(transcript, strategy="fold")

        for msg in to_clear:
            msg.content = placeholder

        target_met = transcript.token_count(model) <= request.target_tokens
        return ReductionOutcome(
            transcript,
            tokens_freed=tokens_freed,
            changed=True,
            strategy="fold",
            target_met=target_met,
        )
