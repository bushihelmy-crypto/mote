"""Token accounting and context-window state.

Wraps Mote's existing ``count_message_tokens`` / ``TOKEN_MAX`` with
window-aware threshold math: effective window,
autocompact buffer scaling, and the warning / error / blocking / should-compact
state used by the loop and UI.

Works on either ``list[Message]`` or already-wire-format ``list[dict]`` so it
can be called both on stored history and on a built request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Sequence, Union

from mote.common.schema import TokenState

if TYPE_CHECKING:
    from mote.common.schema.messages import Message
else:
    from mote.common.schema import Message

from mote.common.const.context import (
    AUTOCOMPACT_BUFFER_TOKENS,
    ERROR_THRESHOLD_BUFFER_TOKENS,
    MANUAL_COMPACT_BUFFER_TOKENS,
    MAX_OUTPUT_TOKENS_FOR_SUMMARY,
    MODEL_CONTEXT_WINDOW_DEFAULT,
    WARNING_THRESHOLD_BUFFER_TOKENS,
)
from mote.common.utils.token_counter import TOKEN_MAX, count_message_tokens

MessageLike = Union[Message, dict]


def _to_dicts(messages: Sequence[MessageLike]) -> list[dict]:
    """Normalize messages to the {role, content} dicts the counter expects.

    Only ``role`` and ``content`` are kept. ``Message.to_dict()`` may also emit
    a ``tool_calls`` key (a list of function-call dicts on native-channel
    assistant turns); the token counter encodes every value it sees, so leaving
    that list in would make it try to ``encode()`` a list and raise. Token
    estimation only needs the textual content, so the call structure is dropped
    here — the small undercount for the tool_calls envelope is acceptable.
    """
    out: list[dict] = []
    for m in messages:
        if isinstance(m, Message):
            d = m.to_dict()
            out.append({"role": d.get("role", "user"), "content": d.get("content", "")})
        elif isinstance(m, dict):
            out.append({"role": m.get("role", "user"), "content": m.get("content", "")})
        else:
            out.append({"role": "user", "content": str(m)})
    return out


def count_tokens(messages: Sequence[MessageLike], model: str) -> int:
    """Count tokens for *messages* under *model* (best-effort via tiktoken)."""
    if not messages:
        return 0
    return count_message_tokens(_to_dicts(messages), model=model)


def context_window(model: str) -> int:
    """The model's full context window, or the default if unknown."""
    return TOKEN_MAX.get(model, MODEL_CONTEXT_WINDOW_DEFAULT)


def effective_window(model: str, *, summary_reserve: int | None = None) -> int:
    """Usable window after reserving room for a compaction summary's output.

    Full window minus the tokens a summary completion may need.
    """
    reserve = summary_reserve if summary_reserve is not None else MAX_OUTPUT_TOKENS_FOR_SUMMARY
    return context_window(model) - reserve


def autocompact_buffer(model: str) -> int:
    """Safety buffer below the effective window, scaled by window size.

    Larger windows reserve more.
    """
    window = effective_window(model)
    if window >= 800_000:
        return 50_000
    if window >= 400_000:
        return 30_000
    return AUTOCOMPACT_BUFFER_TOKENS


def autocompact_threshold(model: str) -> int:
    """Token count at/above which autocompact should fire."""
    return effective_window(model) - autocompact_buffer(model)


def evaluate(
    messages: Sequence[MessageLike],
    model: str,
    *,
    autocompact_enabled: bool = True,
    tokens_freed: int = 0,
    observed_tokens: int | None = None,
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
    window = effective_window(model)
    ac_threshold = autocompact_threshold(model)

    threshold = ac_threshold if autocompact_enabled else window
    warning_threshold = threshold - WARNING_THRESHOLD_BUFFER_TOKENS
    error_threshold = threshold - ERROR_THRESHOLD_BUFFER_TOKENS
    blocking_limit = threshold - MANUAL_COMPACT_BUFFER_TOKENS

    percent_left = max(0, round((threshold - token_count) / threshold * 100)) if threshold > 0 else 0

    return TokenState(
        token_count=token_count,
        model=model,
        effective_window=window,
        autocompact_threshold=ac_threshold,
        percent_left=percent_left,
        above_warning=token_count >= warning_threshold,
        above_error=token_count >= error_threshold,
        above_autocompact=token_count >= ac_threshold,
        at_blocking_limit=token_count >= blocking_limit,
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
