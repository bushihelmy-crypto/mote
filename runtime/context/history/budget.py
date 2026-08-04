"""Token accounting and stored-history context-window state.

Wraps token counting with canonical endpoint context-window metadata and
window-aware threshold math: effective window,
autocompact buffer scaling, and the warning / error / blocking / should-compact
state used by the loop and UI.

Works on either ``list[Message]`` or already-wire-format ``list[dict]`` so it
can be called both on stored history and on a built request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

from mote.contracts.conversation import TokenState

if TYPE_CHECKING:
    from mote.contracts.conversation.messages import Message
else:
    from mote.contracts.conversation import Message

from mote.contracts.conversation.constants import (
    AUTOCOMPACT_BUFFER_TOKENS,
    ERROR_THRESHOLD_BUFFER_TOKENS,
    MANUAL_COMPACT_BUFFER_TOKENS,
    MAX_OUTPUT_TOKENS_FOR_SUMMARY,
    MODEL_CONTEXT_WINDOW_DEFAULT,
    WARNING_THRESHOLD_BUFFER_TOKENS,
)
from mote.contracts.model.invocation import CanonicalMessage
from mote.kernel.inference.tokenization import count_message_tokens
from mote.runtime.context.history.thresholds import ContextBudgetPolicy, evaluate_context_budget


def count_tokens(messages: Sequence[Message], model: str) -> int:
    """Count tokens for *messages* under *model* (best-effort via tiktoken)."""
    if not messages:
        return 0
    canonical = tuple(CanonicalMessage(role=message.role, content=message.content) for message in messages)
    return count_message_tokens(canonical, model=model)


def context_window(model: str, *, context_tokens: int = 0) -> int:
    """Use canonical endpoint metadata, with a conservative test fallback."""
    return context_tokens or MODEL_CONTEXT_WINDOW_DEFAULT


def effective_window(model: str, *, context_tokens: int = 0, summary_reserve: int | None = None) -> int:
    """Usable window after reserving room for a compaction summary's output.

    Full window minus the tokens a summary completion may need.
    """
    requested_reserve = summary_reserve if summary_reserve is not None else MAX_OUTPUT_TOKENS_FOR_SUMMARY
    window = context_window(model, context_tokens=context_tokens)
    reserve = min(requested_reserve, max(1, window // 4))
    return window - reserve


def autocompact_buffer(model: str, *, context_tokens: int = 0) -> int:
    """Safety buffer below the effective window, scaled by window size.

    Larger windows reserve more.
    """
    window = effective_window(model, context_tokens=context_tokens)
    if window >= 800_000:
        return 50_000
    if window >= 400_000:
        return 30_000
    return AUTOCOMPACT_BUFFER_TOKENS


def autocompact_threshold(model: str, *, context_tokens: int = 0) -> int:
    """Token count at/above which autocompact should fire."""
    return max(
        1,
        effective_window(model, context_tokens=context_tokens)
        - autocompact_buffer(model, context_tokens=context_tokens),
    )


def evaluate(
    messages: Sequence[Message],
    model: str,
    *,
    autocompact_enabled: bool = True,
    tokens_freed: int = 0,
    observed_tokens: int | None = None,
    context_tokens: int = 0,
) -> TokenState:
    """Compute the :class:`TokenState` for *messages*.

    Args:
        messages: Stored history or a built request.
        model: Model name (drives the window + thresholds).
        autocompact_enabled: When False, thresholds are measured against the
            full effective window rather than the autocompact threshold.
        tokens_freed: Tokens a prior cheaper pass (microcompact) already
            reclaimed; subtracted before comparing to thresholds.
        observed_tokens: The server-reported token count for the last request
            (see :class:`TokenAccountant`). When given (and positive) it is the
            source of truth for the current size; the tiktoken estimate is only
            the fallback. This closes the drift between our estimate and what the
            provider actually billed.
    """
    if observed_tokens is not None and observed_tokens > 0:
        counted = observed_tokens
    else:
        counted = count_tokens(messages, model)
    token_count = max(0, counted - tokens_freed)
    window = context_window(model, context_tokens=context_tokens)
    policy = ContextBudgetPolicy(
        context_window=window,
        summary_reserve=window - effective_window(model, context_tokens=context_tokens),
        autocompact_threshold=autocompact_threshold(model, context_tokens=context_tokens),
    )
    result = evaluate_context_budget(
        token_count,
        policy,
        autocompact_enabled=autocompact_enabled,
        warning_buffer=WARNING_THRESHOLD_BUFFER_TOKENS,
        error_buffer=ERROR_THRESHOLD_BUFFER_TOKENS,
        blocking_buffer=MANUAL_COMPACT_BUFFER_TOKENS,
    )

    return TokenState(
        token_count=token_count,
        model=model,
        effective_window=policy.effective_window,
        autocompact_threshold=policy.autocompact_threshold,
        percent_left=result.percent_left,
        above_warning=result.above_warning,
        above_error=result.above_error,
        above_autocompact=result.above_autocompact,
        at_blocking_limit=result.at_blocking_limit,
    )


class TokenAccountant:
    """Server-truth token reader with a tiktoken fallback.

    The provider tells us exactly how many tokens the last request cost — that
    figure already sits on the shared ``CostTracker`` as ``last_usage`` (captured
    by ``base_llm._update_costs`` every call). Reading it here means the budget
    math tracks what was actually billed instead of drifting on our estimate, and
    it needs **no new reporting pipeline** — just the ``llm`` the ContextManager
    already holds.

    ``observed()`` returns the last request's total tokens, or ``None`` when there
    is no usage yet (first turn) / no cost manager (standalone / test) — in which
    case the caller falls back to the tiktoken estimate.
    """

    def __init__(self, llm=None) -> None:
        self._llm = llm

    def observed(self) -> int | None:
        """The server-reported total tokens of the last request, or None."""
        cost_manager = getattr(self._llm, "cost_manager", None)
        last_usage = getattr(cost_manager, "last_usage", None)
        if last_usage is None:
            return None
        # ``is_zero`` guards the "no request yet" case (a default TokenUsage).
        is_zero = getattr(last_usage, "is_zero", None)
        if callable(is_zero) and is_zero():
            return None
        total = getattr(last_usage, "total_tokens", 0)
        return int(total) if total else None
