"""Root of the Mote exception hierarchy and retryability markers.

``MoteError`` is the common ancestor for every typed exception in the
codebase. It carries a stable error ``code``, an optional ``cause`` (chained as
``__cause__``), and arbitrary structured ``context``, and serializes via
``to_dict()`` for logging / API responses.

``RetryableError`` and ``NonRetryableError`` are *marker* base classes: a
concrete exception multiply-inherits one of them to flip the ``retryable``
ClassVar. Because the marker precedes ``MoteError`` in the MRO of the
concrete class, the marker's ``retryable`` value wins. This lets retry
predicates (see ``handlers.is_retryable``) decide on *semantics* rather than on
vendor-specific exception tuples.
"""

from __future__ import annotations

from typing import Any, ClassVar

from mote.contracts.foundation.errors.codes import ErrorCode, RecoveryAction


class MoteError(Exception):
    """Base class for all Mote errors.

    Attributes:
        message: Human-readable description.
        code: Stable machine-readable ``ErrorCode``.
        cause: The underlying exception, also chained as ``__cause__``.
        context: Arbitrary structured key/value details for diagnostics.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.UNKNOWN
    retryable: ClassVar[bool] = False
    # Suggested recovery hint. None → derive from ``retryable`` (RETRY/ABORT).
    # Pre-embedded: no retry/failover loop consumes this yet.
    default_recovery: ClassVar[RecoveryAction | None] = None

    def __init__(
        self,
        message: str = "",
        *,
        code: ErrorCode | None = None,
        cause: BaseException | None = None,
        **context: Any,
    ) -> None:
        self.message = message
        self.code = code or self.default_code
        self.cause = cause
        self.context = context
        super().__init__(message)
        if cause is not None:
            self.__cause__ = cause

    @property
    def recovery(self) -> RecoveryAction:
        """Resolved recovery hint: explicit ``default_recovery`` else derived."""
        if self.default_recovery is not None:
            return self.default_recovery
        return RecoveryAction.RETRY if self.retryable else RecoveryAction.ABORT

    def detail(self) -> dict[str, Any]:
        """Structured, presentation-safe payload beyond the human message.

        Overridable hook so a concrete error exposes its own structured fields
        (e.g. a graph router/param/batch error surfaces the offending node,
        param types, or per-node failures) uniformly through ``to_dict`` and
        :class:`~mote.contracts.foundation.errors.report.ErrorReport`. The default is the
        ``context`` mapping, so plain errors need no override.
        """
        return dict(self.context)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-friendly dict for logging / API responses."""
        return {
            "error": type(self).__name__,
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
            "recovery": self.recovery.value,
            "cause": repr(self.cause) if self.cause is not None else None,
            "detail": self.detail(),
            "context": self.context,
        }

    def __str__(self) -> str:
        return self.message or type(self).__name__


class RetryableError(MoteError):
    """Marker base: transient failures that may succeed on retry."""

    retryable: ClassVar[bool] = True


class NonRetryableError(MoteError):
    """Marker base: permanent failures that must not be retried."""

    retryable: ClassVar[bool] = False
