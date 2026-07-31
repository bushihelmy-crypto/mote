"""FoldReducer (FREE) — clear old reconstructable tool bodies/args in place.

The cheap, no-LLM strategy (the former ``microcompact``): once enough
reconstructable model tool-call turns have piled up, replace eligible result
bodies older than the most-recent N complete turns with a short placeholder. The
tool_call↔tool_result pairing is left fully intact — only the text shrinks. Keeping
the pairing intact preserves
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

**Edit whole-file-write args are folded in the SAME pass.** An Edit
whole-file write (``old_string == ""`` so ``new_string`` is the entire file
body) carries its whole content in the *assistant* turn's ``TOOL_CALLS`` args —
a body just as re-readable as a tool result (recovery is a plain Read of the
file, whose path is right there in the call, with the on-disk file as the source
of truth). It used to be folded eagerly at *record time*; it is now folded here,
N results later, exactly with its paired tool result: when that result falls
outside ``keep_recent``, its ``new_string`` is replaced by the neutral
:data:`FOLDED_WRITE_MARKER`, and the tokens they free are pooled with the
result-body savings into the **one** ``clear_at_least`` gate — a single unified
token threshold decides whether the whole (results + writes) fold is worth the
cache write. Deferring to compaction (instead of eager record-time redaction)
keeps the recorded call verbatim while the model is still actively working with
that file, and only sheds it once the turn has aged out of the working set.

A count-gated fold expressed as a pluggable reducer. It mutates
``Message.content`` (result bodies) and ``TOOL_CALLS`` args (Edit writes) in
place (the point: shrink what is kept) and reports the pooled freed-token count.
"""

from __future__ import annotations

from mote.contracts.conversation import ContextManagerConfig, Message
from mote.contracts.conversation.constants import FOLDED_WRITE_MARKER, TOOL_RESULT_CLEARED_MESSAGE
from mote.contracts.conversation.fields import RESOURCE_STICKY, RETENTION, RETENTION_PIN, TOOL_CALL_ID, TOOL_CALLS
from mote.kernel.inference.tokenization import count_string_tokens
from mote.runtime.context.compaction.reducers.base import ReducerCost, ReductionOutcome
from mote.runtime.context.compaction.request import ReductionRequest
from mote.runtime.context.compaction.transcript import Segment, SegmentKind, Transcript


def _is_exempt(msg: Message) -> bool:
    """A message the fold must never touch: a re-projected sticky body
    (``RESOURCE_STICKY``) or a producer-pinned retention (``RETENTION_PIN``).

    The ONE exemption rule shared by both fold paths — result bodies
    (:meth:`FoldReducer.active_results`) and Edit write args
    (:meth:`FoldReducer.foldable_writes`) — so a write and its paired result can
    never disagree on whether their group is foldable. For a ``TOOL_GROUP`` a pin
    on ANY member message pins the whole group (mirrors ``Segment.pinned``, since
    a ``TOOL_GROUP`` is never itself a ``PINNED_KIND``).
    """
    return bool(msg.metadata.get(RESOURCE_STICKY) or msg.metadata.get(RETENTION) == RETENTION_PIN)


class FoldReducer:
    """FREE strategy: tool-call-turn-gated fold of old reconstructable results."""

    cost = ReducerCost.FREE

    def __init__(
        self,
        config: ContextManagerConfig | None = None,
        *,
        model: str = "gpt-4",
        write_fold_names: frozenset[str] = frozenset(),
    ) -> None:
        self._cfg = config or ContextManagerConfig()
        self._model = model
        # Names (primary + aliases) that route to the Edit tool, so a recorded
        # call — which stores the RAW model-emitted name (``write`` / ``Update``
        # all mean Edit) — can be identified without a live executor ref. The
        # Role derives this from the executor and threads it in, mirroring how
        # ``compactable`` is threaded into ``Transcript.from_messages``. Empty by
        # default (standalone/test use) → no write-arg folding until injected.
        self._write_fold_names = write_fold_names

    @staticmethod
    def active_results(transcript: Transcript) -> list[Message]:
        """Foldable tool-result messages still holding real content, in order.

        The single authority on "which results would a fold touch": reconstructable
        (per the segment flag stamped by ``Transcript.from_messages`` — consumed,
        never re-computed), still carrying real content (not already folded to the
        placeholder), and not exempted (sticky re-projected bodies / pinned
        retentions are never folded). Both :meth:`reduce` and the pre-fold pressure
        warning read this, so the count they reason about can never drift apart.
        """
        placeholder = TOOL_RESULT_CLEARED_MESSAGE
        active: list[Message] = []
        for seg in transcript.segments:
            if seg.kind is not SegmentKind.TOOL_GROUP or not seg.reconstructable:
                continue
            for msg in seg.messages:
                if not msg.metadata.get(TOOL_CALL_ID):
                    continue
                # Already folded, or exempt (sticky re-projected body / producer
                # pin) — the shared _is_exempt rule the write path uses too.
                if msg.content == placeholder or _is_exempt(msg):
                    continue
                active.append(msg)
        return active

    @classmethod
    def active_groups(cls, transcript: Transcript) -> list[Segment]:
        """Reconstructable tool-call turns with a live foldable result."""
        active_result_ids = {id(message) for message in cls.active_results(transcript)}
        return [
            segment
            for segment in transcript.segments
            if segment.kind is SegmentKind.TOOL_GROUP
            and segment.reconstructable
            and any(id(message) in active_result_ids for message in segment.messages)
        ]

    def _is_foldable_write(self, call: dict) -> bool:
        """True for an Edit whole-file write whose ``new_string`` can fold.

        Recognises the same shape the record-time limiter used to fold eagerly: an
        Edit call (any alias in ``write_fold_names``) that is a whole-file *write*
        (``old_string == ""`` — INCLUDING an omitted key, which Edit itself treats
        as ``""``). Skips
        anything already folded (``new_string`` is the marker) and anything whose
        ``args`` are not a live dict (e.g. args already spilled to a
        ``<persisted-output>`` string by the >50k record-time persist — that path
        is lossless and idempotent, nothing left to fold here).
        """
        if call.get("name") not in self._write_fold_names:
            return False
        args = call.get("args")
        if not isinstance(args, dict) or args.get("old_string", "") != "":
            return False
        body = args.get("new_string")
        return isinstance(body, str) and body != FOLDED_WRITE_MARKER

    def foldable_writes(
        self,
        transcript: Transcript,
        *,
        cleared_result_ids: frozenset[str] | None = None,
    ) -> list[dict]:
        """Edit whole-file-write call dicts (in order) whose body can still fold.

        The write-arg twin of :meth:`active_results`: walks the same
        reconstructable ``TOOL_GROUP`` segments and returns each assistant call
        holding a not-yet-folded whole-file ``new_string``. Gated on the
        SAME ``reconstructable`` segment flag as results (Edit self-declares
        reconstructable, so a pure-Edit turn qualifies; a turn mixing Edit with a
        non-reconstructable tool is left verbatim — one consistent rule). A group
        with ANY exempt member (:func:`_is_exempt` — the pin/sticky rule results
        use) is skipped whole, so a write and its paired result always agree.
        When ``cleared_result_ids`` is supplied, only calls whose paired result
        crosses the current keep-recent boundary are returned. This prevents a
        separate Edit-only recent window from retaining sparse old writes.
        """
        writes: list[dict] = []
        if not self._write_fold_names:
            return writes
        for seg in transcript.segments:
            if seg.kind is not SegmentKind.TOOL_GROUP or not seg.reconstructable:
                continue
            if any(_is_exempt(m) for m in seg.messages):
                continue
            for call in seg.messages[0].metadata.get(TOOL_CALLS) or []:
                call_id = call.get("id")
                if self._is_foldable_write(call) and (cleared_result_ids is None or call_id in cleared_result_ids):
                    writes.append(call)
        return writes

    async def reduce(self, transcript: Transcript, request: ReductionRequest) -> ReductionOutcome:
        cfg = self._cfg
        model = self._model
        if not cfg.enable_microcompact:
            return ReductionOutcome(transcript, strategy="fold")

        keep_recent = max(1, cfg.microcompact_keep_recent)
        trigger = cfg.microcompact_trigger_threshold
        placeholder = TOOL_RESULT_CLEARED_MESSAGE

        active_groups = self.active_groups(transcript)

        # Count complete model tool-call turns, not individual results. One
        # assistant thinking response may issue several tools in parallel, but
        # that response and its tool batch remain one working-set turn.
        if len(active_groups) <= trigger:
            return ReductionOutcome(transcript, strategy="fold")

        # Protect the last N complete model tool-call turns, including
        # non-reconstructable rounds. Only eligible results in older rounds fold.
        all_tool_groups = [segment for segment in transcript.segments if segment.kind is SegmentKind.TOOL_GROUP]
        protected_group_ids = {id(segment) for segment in all_tool_groups[-keep_recent:]}
        foldable_group_ids = {id(segment) for segment in active_groups if id(segment) not in protected_group_ids}
        to_clear = [
            message
            for segment in transcript.segments
            if id(segment) in foldable_group_ids
            for message in segment.messages
            if message.metadata.get(TOOL_CALL_ID) and message.content != placeholder and not _is_exempt(message)
        ]
        cleared_result_ids = frozenset(
            result_id for msg in to_clear if isinstance((result_id := msg.metadata.get(TOOL_CALL_ID)), str)
        )
        # A whole-file Edit body ages out exactly when its paired tool result
        # does. There is deliberately no second keep_recent window over writes.
        to_fold_writes = self.foldable_writes(
            transcript,
            cleared_result_ids=cleared_result_ids,
        )
        if not to_clear and not to_fold_writes:
            return ReductionOutcome(transcript, strategy="fold")

        # Folding rewrites these bodies AND write args, changing the request prefix
        # and forcing a one-time prompt-cache write. Only worth it if it frees
        # enough to amortize that cost over later turns — otherwise skip and leave
        # the cache warm. Result-body and write-arg savings pool into ONE gate: a
        # single unified token threshold decides the whole pass.
        tokens_freed = sum(count_string_tokens(msg.content, model) for msg in to_clear)
        tokens_freed += sum(count_string_tokens(c["args"]["new_string"], model) for c in to_fold_writes)
        if tokens_freed < cfg.microcompact_clear_at_least:
            return ReductionOutcome(transcript, strategy="fold")

        for msg in to_clear:
            msg.content = placeholder
        for call in to_fold_writes:
            # Rewrite the arg in place (consistent with the result-body rewrite):
            # the marker reads in the environment's voice and points at the paired
            # result + the on-disk file, so re-reading the call never looks like
            # the model typed a placeholder into the file.
            call["args"] = {**call["args"], "new_string": FOLDED_WRITE_MARKER}

        target_met = transcript.token_count(model) <= request.target_tokens
        return ReductionOutcome(
            transcript,
            tokens_freed=tokens_freed,
            changed=True,
            strategy="fold",
            target_met=target_met,
        )
