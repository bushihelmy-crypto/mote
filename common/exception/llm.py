"""LLM / provider tier exceptions.

The ``ConnectionError`` mixin on the connection/empty-response classes is a
safety net: it lets ``except ConnectionError`` blocks and
``retry_if_exception_type((..., ConnectionError))`` predicates catch these typed
errors uniformly.

``LLMError`` carries an optional ``status_code`` (the upstream HTTP status, when
known) so callers/loggers can branch on it without re-parsing the message. Each
concrete error also declares a ``default_recovery`` hint (see
``RecoveryAction``) — pre-embedded metadata for a future failover loop.
"""

from __future__ import annotations

from typing import Any, ClassVar

from mote.common.exception.base import MoteError, NonRetryableError, RetryableError
from mote.common.exception.codes import ErrorCode, RecoveryAction


class LLMError(MoteError):
    """Base for all LLM/provider-layer failures.

    Adds an optional ``status_code`` (upstream HTTP status) and ``retry_after``
    (seconds parsed from a ``Retry-After`` response header, when the provider
    supplied one) on top of the base error fields; both are also mirrored into
    ``context`` so ``to_dict`` surfaces them. ``retry_after`` lets the retry
    backoff honour the provider's advertised cool-off instead of guessing (see
    ``router.llm._retry.wait_retry_after``).
    """

    def __init__(
        self,
        message: str = "",
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
        **kwargs: Any,
    ) -> None:
        self.status_code = status_code
        self.retry_after = retry_after
        if status_code is not None:
            kwargs.setdefault("status_code", status_code)
        if retry_after is not None:
            kwargs.setdefault("retry_after", retry_after)
        super().__init__(message, **kwargs)


class LLMConnectionError(LLMError, RetryableError, ConnectionError):
    """Failed to reach the LLM provider (transient)."""

    default_code: ClassVar[ErrorCode] = ErrorCode.LLM_CONNECTION


class LLMTimeoutError(LLMError, RetryableError):
    """The LLM request timed out (transient)."""

    default_code: ClassVar[ErrorCode] = ErrorCode.LLM_TIMEOUT


class LLMRateLimitError(LLMError, RetryableError):
    """The provider rate-limited the request (transient; backoff then retry)."""

    default_code: ClassVar[ErrorCode] = ErrorCode.LLM_RATE_LIMIT


class LLMEmptyResponseError(LLMError, RetryableError, ConnectionError):
    """The LLM returned an empty response (transient; replaces ConnectionError)."""

    default_code: ClassVar[ErrorCode] = ErrorCode.LLM_EMPTY_RESPONSE


class LLMOverloadedError(LLMError, RetryableError):
    """The provider is overloaded (HTTP 503/529); back off and retry."""

    default_code: ClassVar[ErrorCode] = ErrorCode.LLM_OVERLOADED


class LLMServerError(LLMError, RetryableError):
    """The provider hit an internal server error (HTTP 500/502); retry."""

    default_code: ClassVar[ErrorCode] = ErrorCode.LLM_SERVER


class LLMAuthenticationError(LLMError, NonRetryableError):
    """Authentication with the provider failed (permanent); rotate credential."""

    default_code: ClassVar[ErrorCode] = ErrorCode.LLM_AUTH
    default_recovery: ClassVar[RecoveryAction] = RecoveryAction.ROTATE_CREDENTIAL


class LLMBillingError(LLMError, NonRetryableError):
    """The provider rejected for billing/credit reasons (HTTP 402 / balance).

    Distinct from :class:`~mote.common.exception.resource.NoMoneyException`,
    which is *our own* budget gate. This is the upstream provider refusing on
    its account/credit state — recovery is to rotate to another credential.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.LLM_BILLING
    default_recovery: ClassVar[RecoveryAction] = RecoveryAction.ROTATE_CREDENTIAL


class LLMBadRequestError(LLMError, NonRetryableError):
    """The provider rejected the request as malformed (permanent)."""

    default_code: ClassVar[ErrorCode] = ErrorCode.LLM_BAD_REQUEST


class LLMResponseParseError(LLMError, NonRetryableError):
    """Failed to parse the LLM response (permanent; replaces ValueError/Exception)."""

    default_code: ClassVar[ErrorCode] = ErrorCode.LLM_PARSE


class LLMContentPolicyError(LLMError, NonRetryableError):
    """The provider's safety filter rejected the prompt (deterministic).

    Retrying the unchanged prompt reproduces the same refusal, so the suggested
    recovery is to fall back to a different model/provider.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.LLM_CONTENT_POLICY
    default_recovery: ClassVar[RecoveryAction] = RecoveryAction.FALLBACK


class LLMResourceUnavailableError(LLMError, NonRetryableError):
    """A resource's circuit breaker is OPEN — the call was shed, not attempted.

    Raised by the breaker admit-gate in the LLM chokepoint when a provider/model/
    credential has been failing enough to trip its
    :class:`~mote.common.resilience.CircuitBreaker`. Recovery is FALLBACK: fail
    over to another registered provider (whose own breaker is then checked in
    turn), so a sustained outage on one resource sheds to a healthy one instead
    of retrying the dead one. Never counts as a health failure itself (no call
    reached the provider).
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.LLM_RESOURCE_UNAVAILABLE
    default_recovery: ClassVar[RecoveryAction] = RecoveryAction.FALLBACK


class ContextWindowExceededError(LLMError, NonRetryableError):
    """The request exceeded the model's context window; compress then retry."""

    default_code: ClassVar[ErrorCode] = ErrorCode.LLM_CONTEXT_WINDOW
    default_recovery: ClassVar[RecoveryAction] = RecoveryAction.COMPRESS


class LLMPayloadTooLargeError(LLMError, NonRetryableError):
    """The request body exceeded the provider's size limit (HTTP 413).

    Distinct from :class:`ContextWindowExceededError` (a *token* overflow) — this
    is a *byte* limit on the wire — but the cheapest recovery is the same: shrink
    the payload by compressing the context, then retry.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.LLM_PAYLOAD_TOO_LARGE
    default_recovery: ClassVar[RecoveryAction] = RecoveryAction.COMPRESS


class LLMImageTooLargeError(LLMError, NonRetryableError):
    """A native image part exceeds the provider's per-image limit (e.g. Anthropic 5 MB).

    Recovery is to shrink the oversized image(s) in place and retry the same
    provider; nothing is wired yet, so absent an image transformer this aborts.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.LLM_IMAGE_TOO_LARGE
    default_recovery: ClassVar[RecoveryAction] = RecoveryAction.SHRINK_IMAGE


class LLMMultimodalToolContentError(LLMError, NonRetryableError):
    """The provider rejected list-type (multimodal) content in a tool message.

    Some OpenAI-compatible providers require tool message ``content`` to be a
    plain string. Recovery is to downgrade list-type tool content to text and
    retry; reserved metadata until a transformer is wired.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.LLM_MULTIMODAL_TOOL_CONTENT
    default_recovery: ClassVar[RecoveryAction] = RecoveryAction.DOWNGRADE_TOOL_CONTENT


class LLMInvalidRequestStateError(LLMError, NonRetryableError):
    """The provider rejected some opaque request state carried in the payload.

    Covers replayed encrypted-content blobs, invalid thinking-block signatures,
    or provider-specific tool-schema constructs the backend can't parse. Recovery
    is to strip the offending state and retry; reserved metadata until a
    transformer is wired.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.LLM_INVALID_REQUEST_STATE
    default_recovery: ClassVar[RecoveryAction] = RecoveryAction.STRIP_REQUEST_STATE


class LLMUnusableResponseError(LLMError, NonRetryableError):
    """A successful HTTP-200 response the caller can't use (refusal / empty / wrong shape).

    Raised by the injectable response validator on :class:`BaseLLM` AFTER a
    ``send()`` returns without error, when the model's output is unusable (a bare
    refusal, an empty body, or a structurally wrong shape). Retrying the same
    request against the same provider reproduces the same unusable output, so the
    recovery is FALLBACK — shed to a different model/provider. Being a
    ``NonRetryableError`` it bypasses the inner transient-retry tenacity loop and
    goes straight to the outer ``RecoveryRunner``; FALLBACK never counts as a
    resource-health failure, so a rejection doesn't trip the circuit breaker.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.LLM_UNUSABLE_RESPONSE
    default_recovery: ClassVar[RecoveryAction] = RecoveryAction.FALLBACK
