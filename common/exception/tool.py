"""Tool tier exceptions.

``ToolError`` keeps its name and semantics: it is the exception every tool
raises to signal a recoverable failure, caught by ``ToolExecutor`` and turned
into ``ToolResult(success=False)``. It is re-exported from
``metagpt.executor.tool_result`` so the hundreds of existing ``raise
ToolError(...)`` call sites are automatically upgraded to typed errors without
edits.

``NonRetryableToolError`` additionally inherits ``ValueError`` to preserve
backward compatibility with code that catches ``ValueError`` and with the MCP
retry predicate's historical behavior.
"""

from __future__ import annotations

from typing import ClassVar, Optional

from metagpt.common.exception.base import NonRetryableError, RetryableError
from metagpt.common.exception.codes import ErrorCode


class ToolError(NonRetryableError):
    """Raised by a tool to signal a recoverable failure.

    The executor catches it and produces ``ToolResult(success=False)`` with the
    exception message as ``output``. Tools raise this instead of returning an
    ``"Error:"``-prefixed string, so failure is signalled structurally rather
    than by sniffing the output text. A tool may still return a
    ``ToolResult(success=False)`` directly when it needs to attach structured
    data; raising ToolError is just the ergonomic path for the common
    "fail fast with a message" case.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.TOOL


class ToolValidationError(ToolError):
    """A tool's arguments failed validation."""

    default_code: ClassVar[ErrorCode] = ErrorCode.TOOL_VALIDATION


class ToolNotFoundError(ToolError):
    """The requested tool does not exist."""

    default_code: ClassVar[ErrorCode] = ErrorCode.TOOL_NOT_FOUND


class NonRetryableToolError(ToolError, ValueError):
    """A tool error that should never be retried.

    Inherits ``ValueError`` for backward compatibility with existing
    ``except ValueError`` handlers and the MCP retry logic.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.TOOL_NON_RETRYABLE


class ApplyPatchError(ToolError):
    """A patch could not be parsed (or, from the applier, could not be applied).

    Carries the optional 1-based ``line_no`` where parsing failed so the
    ``apply_patch`` tool can surface codex-style explicit diagnostics.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.TOOL_APPLY_PATCH

    def __init__(self, message: str = "", line_no: Optional[int] = None) -> None:
        super().__init__(message)
        self.line_no = line_no

    def __str__(self) -> str:
        if self.line_no is not None:
            return f"invalid hunk at line {self.line_no}, {self.message}"
        return self.message


class RetryableToolError(RetryableError, ToolError):
    """A transient tool failure worth retrying (network blip, rate-limited
    upstream, temporary lock).

    Still a ``ToolError`` so the executor's existing catch path is unchanged;
    only the ``retryable`` marker flips. ``RetryableError`` is listed FIRST so
    its ``retryable=True`` ClassVar wins over ``ToolError``'s inherited
    ``NonRetryableError`` (``retryable=False``) via MRO; ``recovery`` therefore
    derives to ``RETRY``. Tools opt in by raising this (and only retry when
    wrapped with the ``retryable_tool`` decorator) — a plain ``ToolError`` keeps
    today's abort-on-first-raise semantics.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.TOOL_RETRYABLE
