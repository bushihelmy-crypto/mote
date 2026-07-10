"""Retry predicate + provider-error translator + re-exported helpers.

``is_retryable`` is the single source of truth for "should this exception be
retried?". For typed ``MetaGPTError`` it returns the ``retryable`` marker; for
everything else it falls back to a vendor allowlist (OpenAI transport / rate
limit / 5xx errors plus the stdlib ``ConnectionError``/``TimeoutError`` and a
transient ``json.JSONDecodeError`` carve-out).

``classify_llm_error`` translates a raw provider/OpenAI transport or HTTP error
into a typed :class:`~metagpt.common.exception.llm.LLMError` (status-driven, with
message-pattern refinement only where the HTTP code alone is ambiguous). Applied
at the provider call site, it lets ``is_retryable`` and any future failover loop
work off our own typed hierarchy + ``recovery`` hints rather than re-parsing
vendor exceptions.

``handle_exception`` is re-exported from ``metagpt.common.utils.exceptions``
unchanged (kept in place to minimize churn).
"""

from __future__ import annotations

import json

from metagpt.common.exception.base import MetaGPTError
from metagpt.common.exception.llm import (
    ContextWindowExceededError,
    LLMAuthenticationError,
    LLMBadRequestError,
    LLMBillingError,
    LLMConnectionError,
    LLMContentPolicyError,
    LLMImageTooLargeError,
    LLMInvalidRequestStateError,
    LLMMultimodalToolContentError,
    LLMOverloadedError,
    LLMPayloadTooLargeError,
    LLMRateLimitError,
    LLMServerError,
    LLMTimeoutError,
)
from metagpt.common.utils.exceptions import handle_exception

__all__ = ["is_retryable", "classify_llm_error", "handle_exception"]

# Message/code-pattern refinements used only where the HTTP status alone is
# ambiguous (e.g. OpenAI returns 429 for BOTH transient rate limits AND
# permanent ``insufficient_quota`` billing exhaustion; 400 can be a malformed
# request, a context-window overflow, or a safety-filter rejection).
_BILLING_PATTERNS = (
    "insufficient credit",
    "insufficient_quota",
    "insufficient balance",
    "credit balance",
    "credits exhausted",
    "payment required",
    "billing",
    "out of funds",
    "exceeded your current quota",
    "spending limit",
    "key limit exceeded",
    "account is deactivated",
)
_CONTENT_POLICY_PATTERNS = (
    "content policy",
    "content_policy",
    "content_filter",
    "content management policy",
    "responsible ai",
    "data_inspection_failed",
    "the response was filtered",
    "flagged",
)
_CONTEXT_WINDOW_PATTERNS = (
    "context length",
    "context_length_exceeded",
    "context window",
    "maximum context",
    "too many tokens",
    "reduce the length",
    "string too long",
)
# A native image part is over the provider's per-image ceiling (e.g. Anthropic's
# hard 5 MB). Most providers surface this as a 400 with a specific image message
# *before* the whole request trips the 413 byte limit.
_IMAGE_TOO_LARGE_PATTERNS = (
    "image exceeds",
    "image too large",
    "image_too_large",
    "image size exceeds",
)
# Providers that require tool message ``content`` to be a plain string reject our
# list-type (text + image) content with a 400.
_MULTIMODAL_TOOL_CONTENT_PATTERNS = (
    "tool message content must be a string",
    "tool content must be a string",
    "tool message must be a string",
    "tool_call.content must be string",
    "expected string, got list",
    "expected string, got array",
)
# Opaque request state the backend can't replay/parse: encrypted-content replay
# blobs, invalid thinking-block signatures, provider-specific tool-schema quirks.
_INVALID_REQUEST_STATE_PATTERNS = (
    "invalid encrypted content",
    "encrypted content",
    "thinking signature",
    "signature is invalid",
    "invalid signature",
)
# A relay/gateway (e.g. newapi/one-api) surfacing an UPSTREAM channel failure as
# our HTTP status. The body carries a "bad response status code" marker + an
# upstream request-id rather than a genuine auth/permission rejection: the gateway
# is saying "my upstream returned a bad status", not "your key is invalid". These
# are transient (an upstream channel hiccup fanned out to the concurrent batch),
# so a 401/403 wearing this marker must be RETRIED, not treated as an auth failure
# (which would trigger a no-op credential rotation and abort the turn).
_GATEWAY_RELAY_PATTERNS = (
    "bad_response_status_code",
    "bad response status code",
)


def is_retryable(exc: BaseException | None) -> bool:
    """Return True if ``exc`` represents a transient failure worth retrying."""
    if exc is None:
        return False

    # Control-flow BaseExceptions (CancelledError, KeyboardInterrupt, SystemExit,
    # GeneratorExit) are NOT errors — never retry/swallow them. On Python 3.11+
    # asyncio.CancelledError subclasses BaseException, so a predicate typed on
    # BaseException must guard explicitly or a cancelled turn would be retried.
    if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
        return False
    if type(exc).__name__ == "CancelledError":  # asyncio.CancelledError (BaseException on 3.11+)
        return False

    if isinstance(exc, MetaGPTError):
        return exc.retryable

    # A JSONDecodeError subclasses ValueError but, when it bubbles up from the
    # provider layer (truncated stream, corrupted/empty body, routing-layer
    # mangling), it is a transient hiccup that usually succeeds on retry. This
    # does NOT affect our own LLMResponseParseError (a MetaGPTError, handled
    # above by its non-retryable marker).
    if isinstance(exc, json.JSONDecodeError):
        return True

    # Stdlib transport errors.
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True

    # Vendor fallback for un-migrated / third-party SDK exceptions: transport
    # (APIConnectionError/APITimeoutError), throttling (RateLimitError) and
    # server-side 5xx (InternalServerError) are all transient. The OpenAI and
    # Anthropic SDKs expose the same class names with matching semantics.
    for mod_name in ("openai", "anthropic"):
        try:
            mod = __import__(mod_name)
        except Exception:  # SDK not installed / import failure
            continue
        transient = tuple(
            getattr(mod, n)
            for n in ("APIConnectionError", "APITimeoutError", "RateLimitError", "InternalServerError")
            if hasattr(mod, n)
        )
        if transient and isinstance(exc, transient):
            return True
    return False


def classify_llm_error(exc: BaseException | None) -> MetaGPTError | None:
    """Translate a raw provider/OpenAI error into a typed ``LLMError``.

    Returns the typed exception (chaining ``exc`` as ``cause`` and recording the
    upstream ``status_code``) or ``None`` when ``exc`` is already a
    ``MetaGPTError`` or is not a recognizable provider error — the caller then
    keeps the original. Classification is HTTP-status-driven; message/code
    patterns refine only the cases where the status alone is ambiguous (402/400/
    403/404/429).
    """
    if exc is None or isinstance(exc, MetaGPTError):
        return None

    # Stdlib transport (also covers the OpenAI transport subclasses below).
    if isinstance(exc, TimeoutError):
        return LLMTimeoutError(str(exc), cause=exc)
    if isinstance(exc, ConnectionError):
        return LLMConnectionError(str(exc), cause=exc)

    # Recognize both the OpenAI and Anthropic SDK error hierarchies. Both expose
    # the same class names (APIError/APITimeoutError/APIConnectionError) with a
    # ``status_code`` attribute, so a single status-driven mapping serves both.
    for mod_name in ("openai", "anthropic"):
        try:
            mod = __import__(mod_name)
        except Exception:  # SDK not installed / import failure
            continue
        api_timeout = getattr(mod, "APITimeoutError", None)
        api_connection = getattr(mod, "APIConnectionError", None)
        api_error = getattr(mod, "APIError", None)
        if api_timeout is not None and isinstance(exc, api_timeout):
            return LLMTimeoutError(str(exc), cause=exc)
        if api_connection is not None and isinstance(exc, api_connection):
            return LLMConnectionError(str(exc), cause=exc)
        if api_error is not None and isinstance(exc, api_error):
            return _classify_api_status_error(exc)
    return None


def _classify_api_status_error(exc: BaseException) -> MetaGPTError | None:
    """Map an OpenAI/Anthropic ``APIError`` (with a ``status_code``) to a typed LLMError."""
    status = getattr(exc, "status_code", None)
    message = str(getattr(exc, "message", "") or exc)
    code = str(getattr(exc, "code", "") or "")
    low = f"{message} {code}".lower()

    def _has(patterns: tuple[str, ...]) -> bool:
        return any(p in low for p in patterns)

    if status == 401:
        # A relay gateway middling an upstream channel failure as a 401 — transient.
        if _has(_GATEWAY_RELAY_PATTERNS):
            return LLMOverloadedError(message, status_code=status, cause=exc)
        return LLMAuthenticationError(message, status_code=status, cause=exc)
    if status == 402:
        return LLMBillingError(message, status_code=status, cause=exc)
    if status == 403:
        if _has(_GATEWAY_RELAY_PATTERNS):
            return LLMOverloadedError(message, status_code=status, cause=exc)
        if _has(_BILLING_PATTERNS):
            return LLMBillingError(message, status_code=status, cause=exc)
        return LLMAuthenticationError(message, status_code=status, cause=exc)
    if status == 404:
        if _has(_BILLING_PATTERNS):
            return LLMBillingError(message, status_code=status, cause=exc)
        return LLMBadRequestError(message, status_code=status, cause=exc)
    if status == 413:
        if _has(_IMAGE_TOO_LARGE_PATTERNS):
            return LLMImageTooLargeError(message, status_code=status, cause=exc)
        return LLMPayloadTooLargeError(message, status_code=status, cause=exc)
    if status == 429:
        # OpenAI reuses 429 for permanent ``insufficient_quota`` billing.
        if _has(_BILLING_PATTERNS):
            return LLMBillingError(message, status_code=status, cause=exc)
        return LLMRateLimitError(message, status_code=status, cause=exc)
    if status == 400:
        if _has(_CONTEXT_WINDOW_PATTERNS):
            return ContextWindowExceededError(message, status_code=status, cause=exc)
        if _has(_IMAGE_TOO_LARGE_PATTERNS):
            return LLMImageTooLargeError(message, status_code=status, cause=exc)
        if _has(_MULTIMODAL_TOOL_CONTENT_PATTERNS):
            return LLMMultimodalToolContentError(message, status_code=status, cause=exc)
        if _has(_INVALID_REQUEST_STATE_PATTERNS):
            return LLMInvalidRequestStateError(message, status_code=status, cause=exc)
        if _has(_CONTENT_POLICY_PATTERNS):
            return LLMContentPolicyError(message, status_code=status, cause=exc)
        if _has(_BILLING_PATTERNS):
            return LLMBillingError(message, status_code=status, cause=exc)
        return LLMBadRequestError(message, status_code=status, cause=exc)
    if status in (500, 502):
        return LLMServerError(message, status_code=status, cause=exc)
    if status in (503, 529):
        return LLMOverloadedError(message, status_code=status, cause=exc)
    if isinstance(status, int):
        if 400 <= status < 500:
            if _has(_CONTENT_POLICY_PATTERNS):
                return LLMContentPolicyError(message, status_code=status, cause=exc)
            return LLMBadRequestError(message, status_code=status, cause=exc)
        if 500 <= status < 600:
            return LLMServerError(message, status_code=status, cause=exc)

    # An APIError with no usable status — leave to the caller / is_retryable.
    return None
