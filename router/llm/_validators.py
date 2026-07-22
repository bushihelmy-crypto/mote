"""Response validators for RESPONSE-based FALLBACK (③).

A response validator is a ``(result) -> Optional[str]`` callable checked by
:class:`BaseLLM` AFTER a successful ``send()``. Returning a non-empty string
means the HTTP-200 response is unusable — the provider then raises a
FALLBACK-classified :class:`LLMUnusableResponseError` so the recovery loop sheds
to another provider (a same-request retry would reproduce the same output).

:func:`default_response_validator` is the conservative built-in: it flags ONLY a
completely empty answer (no text AND no tool calls), which is unambiguously
unusable regardless of task. Refusal / wrong-shape detection is deliberately left
OUT of the default (string-matching refusals is model-specific and false-positive
prone) — a caller that wants it composes its own validator and assigns it to
``LLMRouter.response_validator``.
"""

from __future__ import annotations

from typing import Any, Optional

from mote.router.llm.llm_response import LLMResponse


def default_response_validator(result: Any) -> Optional[str]:
    """Reject a completely empty completion; otherwise accept (return ``None``).

    Empty = no assistant text and (for the native tool-use shape) no tool calls.
    A blank body with no structured calls carries no signal for the turn to act
    on, so shedding to another provider is strictly better than looping on it.
    """
    if isinstance(result, LLMResponse):
        if not (result.content or "").strip() and not result.tool_calls:
            return "empty response (no text, no tool calls)"
        return None
    if isinstance(result, str):
        if not result.strip():
            return "empty response (no text)"
        return None
    # Unknown shape: don't second-guess it — accept and let downstream handle it.
    return None
