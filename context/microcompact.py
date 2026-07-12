"""History-level microcompact — fold old tool results in place.

Ported from Claude Code's ``services/compact/microCompact.ts`` (+ the count
trigger from ``cachedMicrocompact.ts``). Microcompact is the *cheap* context
reduction: it does NOT summarize or call the LLM. It walks the stored history,
finds tool-result messages from read-ish / search-ish / file tools, and once
enough have piled up, replaces the *content* of all but the most-recent N with a
short placeholder (``TOOL_RESULT_CLEARED_MESSAGE``). The tool_call ↔ tool_result
pairing is left intact (only the text shrinks), so the conversation stays valid.

Why a content-clear (not a delete): CC's primary path uses the Anthropic
``cache_edits`` API to drop old tool results without breaking the warm prefix
cache. MetaGPT has no such API, so we port CC's *fallback* (time-based) action —
mutate the result content directly — but gate it on CC's count trigger
(``trigger_threshold`` / ``keep_recent``) rather than a cache-expiry timer.

CC's COMPACTABLE_TOOLS set: only these tools' results are foldable (their output
is reconstructable / re-runnable). Conversational tools (AskUserQuestion), the
terminal End, Sleep, etc. are never touched.

This module mutates ``Message.content`` in place on the stored history — that is
the point (shrink what is kept). It reports how many tokens were freed so the
caller (token_budget.evaluate / the autocompact decision) can subtract them.
"""

from __future__ import annotations

from metagpt.common.schema import MicrocompactResult, Message
from metagpt.common.const import TOOL_CALL_ID, TOOL_CALLS
from metagpt.common.const.context import TOOL_RESULT_CLEARED_MESSAGE
from metagpt.common.schema import ContextManagerConfig
from metagpt.common.utils.token_counter import count_string_tokens

# CC ``COMPACTABLE_TOOLS`` (microCompact.ts): only fold results from these
# read/search/file tools — their output can be regenerated, so clearing it is
# safe. Names are the primary tool names the model invokes (what lands in
# ``TOOL_CALLS[*].name``). WebSearch/WebFetch are listed for forward-compat with
# CC even though MetaGPT has no such tools yet (harmless: they just never match).
COMPACTABLE_TOOLS: frozenset[str] = frozenset(
    {
        "Read",
        "Bash",
        "Grep",
        "Glob",
        "Write",
        "Edit",
        "WebSearch",
        "WebFetch",
    }
)


def _collect_compactable_tool_ids(messages: list[Message], compactable: frozenset[str]) -> list[str]:
    """Tool-use ids of compactable tools, in encounter order (CC collectCompactableToolIds)."""
    ids: list[str] = []
    for m in messages:
        calls = m.metadata.get(TOOL_CALLS)
        if not calls:
            continue
        for c in calls:
            if c.get("name") in compactable and c.get("id"):
                ids.append(c["id"])
    return ids


def _index_results_by_call_id(messages: list[Message]) -> dict[str, Message]:
    """Map tool_call_id -> the tool-result message it belongs to."""
    by_id: dict[str, Message] = {}
    for m in messages:
        cid = m.metadata.get(TOOL_CALL_ID)
        if cid:
            by_id[cid] = m
    return by_id


def microcompact(
    messages: list[Message],
    config: ContextManagerConfig | None = None,
    *,
    model: str = "gpt-4",
    compactable: frozenset[str] = COMPACTABLE_TOOLS,
) -> MicrocompactResult:
    """Fold old compactable tool results in *messages* in place.

    Mirrors CC's count-gated content-clear:
      1. Collect compactable tool-use ids in order.
      2. ``active`` = those whose result is not already cleared.
      3. Only fire when ``len(active) > trigger_threshold`` (else cheap no-op).
      4. Keep the last ``max(1, keep_recent)`` active results; clear the rest by
         replacing their content with ``TOOL_RESULT_CLEARED_MESSAGE``.

    The floor of 1 on ``keep_recent`` matches CC: clearing *every* result would
    leave the model with zero working context.

    Args:
        messages: Stored history (mutated in place).
        config: Knobs; defaults reproduce CC (keep 5, trigger at 10).
        model: Used only to estimate freed tokens (tiktoken, falls back to
            cl100k_base for unknown models).
        compactable: Override the set of foldable tool names (tests/tuning).

    Returns:
        :class:`MicrocompactResult` — same list, freed-token count, cleared ids.
    """
    cfg = config or ContextManagerConfig()
    if not cfg.enable_microcompact:
        return MicrocompactResult(messages)

    keep_recent = max(1, cfg.microcompact_keep_recent)
    trigger = cfg.microcompact_trigger_threshold
    placeholder = TOOL_RESULT_CLEARED_MESSAGE

    ids = _collect_compactable_tool_ids(messages, compactable)
    result_by_id = _index_results_by_call_id(messages)

    # active = compactable results that still hold real content (CC excludes
    # already-deleted refs the same way).
    active = [i for i in ids if i in result_by_id and result_by_id[i].content != placeholder]
    if len(active) <= trigger:
        return MicrocompactResult(messages)

    to_clear = active[: len(active) - keep_recent]
    if not to_clear:
        return MicrocompactResult(messages)

    tokens_freed = 0
    cleared: list[str] = []
    for cid in to_clear:
        msg = result_by_id[cid]
        tokens_freed += count_string_tokens(msg.content, model)
        msg.content = placeholder
        cleared.append(cid)

    return MicrocompactResult(messages, tokens_freed=tokens_freed, cleared_tool_use_ids=cleared)
