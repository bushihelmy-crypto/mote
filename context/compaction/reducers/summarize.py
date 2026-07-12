"""SummarizeReducer (LLM) — summarize the head, keep a verbatim tail.

The expensive strategy: when cheaper folding did not free enough, ask the LLM to
write a structured summary of the older turns, then rebuild the history as
``[pinned] + [summary] + [sticky] + tail``.

The head/tail split goes through :meth:`Transcript.split_keep_tail`, which only
ever cuts on a segment boundary, so it can never land between an assistant
``tool_use`` and its ``tool_result`` (an orphaned ``tool_result`` in the kept
tail would make Anthropic 400).

Safeguards:
- the circuit breaker (stop after N consecutive summarize failures),
- ``sticky_provider`` re-projection (loaded capability bodies survive the head
  being discarded — re-inserted right after the summary),
- partial vs full compact-prompt selection (a tail is kept → partial "up_to").

``rehydrate_provider`` re-projection re-reads the recent working-set files from
disk (re-inserted after the sticky bodies) so the model resumes with a fresh
view of the files it was editing, not just the summary's paraphrase.
"""

from __future__ import annotations

import mote.context.prompt as compact_prompt
from mote.common.logs import logger
from mote.common.schema import ContextManagerConfig, Message, UserMessage
from mote.context.compaction.reducers.base import ReducerCost, ReductionOutcome
from mote.context.compaction.request import ReductionRequest
from mote.context.compaction.transcript import Transcript


def _summary_message(summary: str, *, recent_preserved: bool) -> UserMessage:
    """The single user message that replaces the summarized head."""
    content = compact_prompt.get_compact_user_summary_message(
        summary,
        suppress_follow_up_questions=True,
        recent_messages_preserved=recent_preserved,
    )
    return UserMessage(content=content)


class SummarizeReducer:
    """LLM strategy: summarize the head into one message, keep the recent tail."""

    cost = ReducerCost.LLM

    def __init__(
        self,
        llm,
        config: ContextManagerConfig | None = None,
        *,
        model: str = "gpt-4",
        sticky_provider=None,
        rehydrate_provider=None,
    ) -> None:
        self._llm = llm
        self._cfg = config or ContextManagerConfig()
        self._model = model
        # Optional zero-arg callable returning sticky Messages to re-insert right
        # after the summary (loaded Skill bodies from the ResourceRegistry).
        self._sticky_provider = sticky_provider
        # Optional zero-arg callable returning file-snapshot Messages (the recent
        # working set re-read from disk) to re-insert after the summary — the
        # eager counterpart to the lazy "re-read if you need specifics" advisory.
        self._rehydrate_provider = rehydrate_provider
        # Circuit breaker + per-pass override, threaded across calls / set by the
        # engine from a PreCompact hook's additional_context.
        self.consecutive_failures = 0
        self.custom_instructions: str | None = None

    def _collect_sticky(self) -> list[Message]:
        """Materialize the sticky re-projection messages, best-effort."""
        if self._sticky_provider is None:
            return []
        try:
            return list(self._sticky_provider() or [])
        except Exception as e:  # noqa: BLE001 — re-projection must not break compaction
            logger.warning(f"summarize: sticky re-projection failed: {e}")
            return []

    def _collect_rehydrated(self, preserved: list[Message]) -> list[Message]:
        """Materialize the re-read working-set file snapshots, best-effort.

        Passes the preserved tail so the rehydrator can skip files that tail
        already shows (dedup). Falls back to a no-arg call for providers that
        don't accept it (older zero-arg callables / test doubles).
        """
        if self._rehydrate_provider is None:
            return []
        try:
            try:
                result = self._rehydrate_provider(preserved)
            except TypeError:
                # Provider doesn't take the preserved tail — call it bare.
                result = self._rehydrate_provider()
            return list(result or [])
        except Exception as e:  # noqa: BLE001 — rehydration must not break compaction
            logger.warning(f"summarize: file rehydration failed: {e}")
            return []

    async def reduce(self, transcript: Transcript, request: ReductionRequest) -> ReductionOutcome:
        cfg = self._cfg
        model = self._model
        if not cfg.enable_autocompact or self._llm is None:
            return ReductionOutcome(transcript, strategy="summarize")
        # Circuit breaker: stop after too many consecutive summarize failures.
        if self.consecutive_failures >= cfg.max_consecutive_failures:
            return ReductionOutcome(transcript, strategy="summarize")

        split = transcript.split_keep_tail(
            keep_tail_messages=cfg.keep_tail_messages,
            keep_tail_tokens=cfg.keep_tail_tokens,
            model=model,
        )
        if split >= len(transcript.segments):
            return ReductionOutcome(transcript, strategy="summarize")  # too short to carve

        head_segments = transcript.segments[:split]
        tail_segments = transcript.segments[split:]

        # Pinned head segments (system anchors) are never summarized — they are
        # extracted and re-prepended around the summary.
        pinned_head = [s for s in head_segments if s.pinned]
        summarizable = [s for s in head_segments if not s.pinned]
        if not summarizable:
            return ReductionOutcome(transcript, strategy="summarize")

        head_msgs: list[Message] = []
        for s in summarizable:
            head_msgs.extend(s.messages)
        tail_msgs = Transcript(tail_segments).to_messages()
        pinned_msgs = Transcript(pinned_head).to_messages()

        recent_preserved = bool(tail_msgs)
        instruction = (
            compact_prompt.get_partial_compact_prompt(self.custom_instructions)
            if recent_preserved
            else compact_prompt.get_compact_prompt(self.custom_instructions)
        )

        try:
            summary = await self._llm.aask(msg=head_msgs, system_msgs=[instruction], stream=False)
        except Exception as e:  # noqa: BLE001 — any summarize failure trips the breaker
            logger.warning(f"summarize: summarize failed: {e}")
            self.consecutive_failures += 1
            return ReductionOutcome(transcript, strategy="summarize")

        if not summary or not summary.strip():
            self.consecutive_failures += 1
            return ReductionOutcome(transcript, strategy="summarize")

        sticky = self._collect_sticky()
        rehydrated = self._collect_rehydrated(tail_msgs)
        rebuilt = [
            *pinned_msgs,
            _summary_message(summary, recent_preserved=recent_preserved),
            *sticky,
            *rehydrated,
            *tail_msgs,
        ]
        new_transcript = Transcript.from_messages(rebuilt)

        self.consecutive_failures = 0  # reset on success
        pre = transcript.token_count(model)
        post = new_transcript.token_count(model)
        return ReductionOutcome(
            new_transcript,
            tokens_freed=max(0, pre - post),
            changed=True,
            strategy="summarize",
            target_met=post <= request.target_tokens,
            summary=summary,
        )
