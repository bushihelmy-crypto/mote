"""Media-generation tier exceptions (``mote.product.toolsets.builtin.generate_media``).

The upstream async-task media API (image / audio / music / video) returns a
``status`` field that is either ``completed`` or ``failed``. A ``failed`` task
rarely carries a useful reason — most are transient backend hiccups (queue
overload, a flaky GPU worker, a timed-out render) that succeed on a fresh
re-submission. The historical code raised a bare ``RuntimeError`` for every
failure, which :func:`~mote.runtime.errors.classification.is_retryable`
classifies as **permanent** → the bggraph engine aborts a node with ``attempt=0``
and never re-submits.

Policy (per product decision): **retry everything except an explicitly permanent
error code**. So the default media failure is :class:`MediaGenerationError`
(retryable → the engine consumes its auto-retry budget, re-running the node which
re-submits the task). Only a failure whose error code is in
:data:`PERMANENT_ERROR_CODES` (or matches :data:`PERMANENT_MESSAGE_PATTERNS`) is
raised as :class:`PermanentMediaGenerationError` (non-retryable → fail fast,
surfaced to the model/user immediately because retrying would just burn budget).
"""

from __future__ import annotations

from typing import ClassVar, Optional

from mote.contracts.foundation.errors.base import RetryableError
from mote.contracts.foundation.errors.codes import ErrorCode

# Upstream error codes that are permanent — retrying cannot succeed, so fail
# fast. Everything NOT in this set is treated as transient and retried. Keep the
# set conservative: when in doubt, retry (the product decision is "try whatever
# can be tried, abort only on an explicit permanent code").
PERMANENT_ERROR_CODES: frozenset[str] = frozenset(
    {
        # auth / quota / billing — a fresh submit hits the same wall
        "401",
        "402",
        "403",
        "invalid_api_key",
        "insufficient_quota",
        "insufficient_balance",
        "billing",
        "account_deactivated",
        "permission_denied",
        # request is malformed / rejected — identical re-submit will be rejected too
        "400",
        "invalid_request",
        "invalid_parameter",
        "invalid_prompt",
        "unsupported",
        "not_found",
        "404",
        # content moderation — the same prompt will always be blocked
        "content_policy",
        "content_filter",
        "content_moderation",
        "sensitive_content",
        "prohibited_content",
        "data_inspection_failed",
    }
)

# Message-substring fallback for upstreams that don't return a stable code,
# only a human-readable ``error.message``. Matched case-insensitively.
PERMANENT_MESSAGE_PATTERNS: tuple[str, ...] = (
    "insufficient credit",
    "insufficient_quota",
    "insufficient balance",
    "quota exceeded",
    "billing",
    "payment required",
    "api key",
    "unauthorized",
    "permission denied",
    "content policy",
    "content_policy",
    "content filter",
    "content moderation",
    "prohibited",
    "sensitive content",
    "violates",
    "invalid prompt",
    "invalid parameter",
    "invalid request",
    "unsupported",
)


class MediaGenerationError(RetryableError):
    """A media-generation task failed transiently — worth re-submitting.

    This is the DEFAULT for any upstream ``status == "failed"`` (or an empty /
    unparseable failure). It is retryable so the bggraph engine's auto-retry
    budget re-runs the node, which re-submits a fresh task.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.MEDIA_GENERATION_RETRYABLE

    def __init__(
        self,
        message: str = "",
        *,
        task_id: Optional[str] = None,
        upstream_code: Optional[str] = None,
        cause: BaseException | None = None,
        **context,
    ) -> None:
        super().__init__(message, cause=cause, **context)
        self.task_id = task_id
        self.upstream_code = upstream_code


class PermanentMediaGenerationError(MediaGenerationError):
    """A media-generation failure that re-submission cannot fix.

    Raised only when the upstream error code/message is explicitly permanent
    (auth, quota, malformed request, content moderation). Still a
    :class:`MediaGenerationError` subclass (so ``_failure_entry``'s isinstance
    check and the per-item catch path are unchanged), but it pins
    ``retryable = False`` so ``recovery`` derives to ABORT and the bggraph engine
    fails fast instead of burning the auto-retry budget. ``retryable`` is set
    explicitly rather than via a ``NonRetryableError`` mixin because
    ``MediaGenerationError`` already extends ``RetryableError`` (which would win
    the MRO and leave ``retryable=True``).
    """

    retryable: ClassVar[bool] = False
    default_code: ClassVar[ErrorCode] = ErrorCode.MEDIA_GENERATION_PERMANENT


def is_permanent_media_failure(code: Optional[str], message: str = "") -> bool:
    """Decide whether an upstream media failure is permanent (don't retry).

    Permanent iff the normalized ``code`` is in :data:`PERMANENT_ERROR_CODES`
    or the lowercased ``message`` contains a :data:`PERMANENT_MESSAGE_PATTERNS`
    substring. Everything else is transient (retry).
    """
    norm_code = (code or "").strip().lower()
    if norm_code and norm_code in PERMANENT_ERROR_CODES:
        return True
    low = message.lower()
    return any(p in low for p in PERMANENT_MESSAGE_PATTERNS)


def classify_media_failure(
    message: str,
    *,
    task_id: Optional[str] = None,
    code: Optional[str] = None,
) -> MediaGenerationError:
    """Build the right typed media error from an upstream failure.

    Returns :class:`PermanentMediaGenerationError` when the code/message is
    explicitly permanent, else the retryable :class:`MediaGenerationError`.
    """
    cls = PermanentMediaGenerationError if is_permanent_media_failure(code, message) else MediaGenerationError
    return cls(message, task_id=task_id, upstream_code=code)
