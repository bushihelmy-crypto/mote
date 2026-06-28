"""History-level autocompact — summarize old turns & rebuild the conversation.

Ported from Claude Code ``services/compact/autoCompact.ts`` + ``compact.ts``.
Autocompact is the *expensive* context reduction: when the stored history nears
the model's window, it asks the LLM to write a structured summary of the older
turns, then rebuilds the history as ``[summary] + recent-tail`` so work can
continue with a much smaller prompt.

Relationship to the other scopes:
- microcompact (cheap, no LLM) runs first and folds old tool-result *bodies*.
  Its freed-token count is fed in here via ``tokens_freed`` so we don't fire a
  pricey summarize when folding already bought enough headroom.
- The threshold math (effective window, buffers, the should-fire decision)
  lives in ``token_budget.py``; this module owns the *action*.

Design choices vs CC:
- We always keep a recent tail verbatim (CC ``partialCompact`` 'up_to'): the
  summary is placed BEFORE the kept tail, so we use the partial/up_to prompt.
  Tail size = ``keep_tail_messages`` messages or ``keep_tail_tokens`` tokens,
  whichever keeps more, but never the whole history.
- The summary becomes one ``UserMessage`` (CC's ``summaryMessages``) carrying
  the continued-session preface. No boundary marker / attachments / hooks —
  those are CC plumbing with no MetaGPT equivalent yet.
- Circuit breaker: after ``max_consecutive_failures`` summarize errors in a
  row we stop trying (CC ``MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES``).

The summarize call goes through ``llm.aask`` with the older turns as the
conversation and the compact prompt as the final user instruction. Any LLM with
an ``async aask(msg, system_msgs=...)`` works; tests pass a fake.
"""

from __future__ import annotations

from metagpt.common.logs import logger
from metagpt.common.schema import AutocompactResult, Message, UserMessage
import metagpt.context.prompt as compact_prompt
import metagpt.context.token_budget as token_budget
from metagpt.common.schema import ContextManagerConfig
from metagpt.common.utils.token_counter import count_string_tokens


def _split_keep_tail(messages: list[Message], cfg: ContextManagerConfig, model: str) -> int:
    """Index where the preserved tail begins.

    The tail is the recent messages kept verbatim. We keep at least
    ``keep_tail_messages`` messages AND at least ``keep_tail_tokens`` tokens
    (whichever reaches further back), but never the whole conversation — there
    must be a head left to summarize, else there's nothing to compact.

    Returns the split index ``i`` such that ``messages[:i]`` is summarized and
    ``messages[i:]`` is kept. Returns ``len(messages)`` (keep all) when the
    history is too short to bother.
    """
    n = len(messages)
    if n <= cfg.keep_tail_messages + 1:
        return n  # nothing meaningful to summarize

    # Walk backward accumulating tokens until both bounds are satisfied.
    tail_tokens = 0
    split = n
    for i in range(n - 1, -1, -1):
        kept = n - i  # messages in the tail if we split here
        tail_tokens += count_string_tokens(messages[i].content or "", model)
        split = i
        if kept >= cfg.keep_tail_messages and tail_tokens >= cfg.keep_tail_tokens:
            break

    # Always leave at least one head message to summarize.
    if split <= 0:
        split = 1
    return split


def _summary_message(summary: str, *, recent_preserved: bool) -> UserMessage:
    """The single user message that replaces the summarized head."""
    content = compact_prompt.get_compact_user_summary_message(
        summary,
        suppress_follow_up_questions=True,
        recent_messages_preserved=recent_preserved,
    )
    return UserMessage(content=content)


async def autocompact(
    messages: list[Message],
    llm,
    config: ContextManagerConfig | None = None,
    *,
    model: str | None = None,
    tokens_freed: int = 0,
    consecutive_failures: int = 0,
    custom_instructions: str | None = None,
) -> AutocompactResult:
    """Summarize old turns and rebuild *messages* when over the autocompact threshold.

    Args:
        messages: Stored history. Not mutated; a new list is returned on compact.
        llm: Anything with ``async aask(msg, system_msgs=None) -> str``. The
            summarize prompt is passed as ``system_msgs`` and the head turns as
            ``msg`` so the model summarizes the conversation it is shown.
        config: Knobs; defaults reproduce CC.
        model: Model name for threshold + token math. Falls back to
            ``llm.model`` then a generic default.
        tokens_freed: Tokens a prior microcompact pass already reclaimed —
            subtracted before the threshold check (don't summarize if folding
            was enough).
        consecutive_failures: Running failure count threaded by the caller for
            the circuit breaker.
        custom_instructions: Extra summarization guidance appended to the prompt.

    Returns:
        :class:`AutocompactResult`. ``compacted=False`` (with ``messages``
        unchanged) when under threshold, disabled, breaker tripped, history too
        short, or the summarize call failed.
    """
    cfg = config or ContextManagerConfig()
    model = model or getattr(llm, "model", None) or "gpt-4"

    if not cfg.enable_autocompact:
        return AutocompactResult(messages, consecutive_failures=consecutive_failures)

    # Circuit breaker (CC MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES).
    if consecutive_failures >= cfg.max_consecutive_failures:
        return AutocompactResult(messages, consecutive_failures=consecutive_failures)

    state = token_budget.evaluate(messages, model, autocompact_enabled=True, tokens_freed=tokens_freed)
    if not state.should_autocompact:
        return AutocompactResult(
            messages, pre_compact_tokens=state.token_count, consecutive_failures=consecutive_failures
        )

    split = _split_keep_tail(messages, cfg, model)
    if split >= len(messages):
        # Too short to carve a head/tail — nothing to do.
        return AutocompactResult(
            messages, pre_compact_tokens=state.token_count, consecutive_failures=consecutive_failures
        )

    head = messages[:split]
    tail = messages[split:]
    recent_preserved = bool(tail)
    instruction = (
        compact_prompt.get_partial_compact_prompt(custom_instructions)
        if recent_preserved
        else compact_prompt.get_compact_prompt(custom_instructions)
    )

    try:
        summary = await llm.aask(msg=head, system_msgs=[instruction], stream=False)
    except Exception as e:  # noqa: BLE001 — any summarize failure trips the breaker
        logger.warning(f"autocompact: summarize failed: {e}")
        return AutocompactResult(
            messages,
            pre_compact_tokens=state.token_count,
            consecutive_failures=consecutive_failures + 1,
            error=str(e),
        )

    if not summary or not summary.strip():
        return AutocompactResult(
            messages,
            pre_compact_tokens=state.token_count,
            consecutive_failures=consecutive_failures + 1,
            error="empty summary",
        )

    rebuilt: list[Message] = [_summary_message(summary, recent_preserved=recent_preserved), *tail]
    post = token_budget.count_tokens(rebuilt, model)
    return AutocompactResult(
        rebuilt,
        compacted=True,
        summary=summary,
        pre_compact_tokens=state.token_count,
        post_compact_tokens=post,
        consecutive_failures=0,  # reset on success (CC)
    )
