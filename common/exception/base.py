"""Root of the MetaGPT exception hierarchy + retryable/non-retryable markers.

``MetaGPTError`` is the common ancestor for every typed exception in the
codebase. It carries a stable error ``code``, an optional ``cause`` (chained as
``__cause__``), and arbitrary structured ``context``, and serializes via
``to_dict()`` for logging / API responses.

``RetryableError`` and ``NonRetryableError`` are *marker* base classes: a
concrete exception multiply-inherits one of them to flip the ``retryable``
ClassVar. Because the marker precedes ``MetaGPTError`` in the MRO of the
concrete class, the marker's ``retryable`` value wins. This lets retry
predicates (see ``handlers.is_retryable``) decide on *semantics* rather than on
vendor-specific exception tuples.
"""

from __future__ import annotations

import pickle
from typing import Any, ClassVar

from metagpt.common.exception.codes import ErrorCode, RecoveryAction


class _SanitizedCause(Exception):
    """Picklable stand-in for a ``cause`` that cannot survive an unpickle.

    Some third-party exceptions (notably openai's ``APIStatusError``) pickle
    (``dumps``) fine but fail to ``loads`` because their ``__init__`` demands
    keyword-only args the pickle machinery never supplies. When a
    ``MetaGPTError`` carrying such a cause is routed through loguru's
    ``enqueue=True`` queue (which pickles every record), the consumer side
    crashes on load. We swap the offending cause for this repr-only placeholder
    *only during pickling*; the in-process error keeps its real cause.
    """

    def __init__(self, original_repr: str = "") -> None:
        self.original_repr = original_repr
        super().__init__(original_repr)

    def __repr__(self) -> str:
        return self.original_repr or "_SanitizedCause()"


def _is_picklable(obj: Any) -> bool:
    """True if ``obj`` survives a full pickle round-trip (dumps *and* loads)."""
    try:
        pickle.loads(pickle.dumps(obj))
        return True
    except Exception:
        return False


def _rebuild_metagpt_error(cls: type, args: tuple, state: dict) -> "MetaGPTError":
    """Reconstruct a pickled ``MetaGPTError`` without re-running ``__init__``.

    Subclass ``__init__`` signatures vary (e.g. ``LLMError`` wants keyword-only
    ``status_code``), so we bypass them: build a bare instance, restore ``args``
    and ``__dict__``, then re-link ``__cause__`` (a C-slot that pickle drops).
    """
    obj = cls.__new__(cls)
    obj.args = args
    obj.__dict__.update(state)
    cause = state.get("cause")
    if cause is not None:
        obj.__cause__ = cause
    return obj


class MetaGPTError(Exception):
    """Base class for all MetaGPT errors.

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
        :class:`~metagpt.common.exception.report.ErrorReport`. The default is the
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

    def __reduce__(self):
        """Make the error picklable across processes (loguru ``enqueue=True``).

        Default exception pickling stores ``__dict__`` verbatim, so a
        non-loadable ``cause`` (e.g. openai ``APIStatusError``) breaks the
        consumer-side ``loads``. We copy the state and replace such a cause with
        a repr-only :class:`_SanitizedCause`. In-process behavior (traceback,
        ``to_dict``) is untouched — ``__reduce__`` only runs when pickling.
        """
        state = dict(self.__dict__)
        cause = state.get("cause")
        if cause is not None and not _is_picklable(cause):
            state["cause"] = _SanitizedCause(repr(cause))
        return (_rebuild_metagpt_error, (type(self), self.args, state))


class RetryableError(MetaGPTError):
    """Marker base: transient failures that may succeed on retry."""

    retryable: ClassVar[bool] = True


class NonRetryableError(MetaGPTError):
    """Marker base: permanent failures that must not be retried."""

    retryable: ClassVar[bool] = False
