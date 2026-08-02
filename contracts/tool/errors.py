"""Tool errors.

``ToolError`` is the exception every tool raises to signal a recoverable
failure, caught by ``ToolExecutor`` and turned into ``ToolResult(success=False)``.
Tool code imports it from this authoritative Contracts module.

``NonRetryableToolError`` additionally inherits ``ValueError`` so code that
catches ``ValueError`` and the MCP retry predicate treat it as non-retryable.
"""

from __future__ import annotations

from typing import ClassVar

from mote.contracts.foundation.errors.base import NonRetryableError, RetryableError
from mote.contracts.foundation.errors.codes import ErrorCode


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


class ToolPermissionDeniedError(ToolError):
    """A tool call was blocked before execution by the permission gate.

    Covers both the PreToolUse hook ``deny`` and the permission engine ``deny``
    — a pre-flight rejection that never reaches ``tool.call()``. Routed through
    the shared error contract so a denial surfaces as the same uniform
    ``<error>`` block (carrying ``code=TOOL_PERMISSION_DENIED``) as any other
    tool failure, instead of an ad-hoc ``[PERMISSION DENIED] …`` string.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.TOOL_PERMISSION_DENIED


class ToolNotConfiguredError(ToolError):
    """A tool cannot run because a service/model it depends on is not configured.

    Raised when the prerequisite is a *configuration* gap rather than a bad
    argument or a transient failure — the routed model does not support the
    capability (e.g. the ``web_search`` task model has no server-side search) or
    a required service is unset (e.g. an empty ``multimodal.*_generation``
    endpoint/key). The message should name the exact config path the user must
    fill so the model surfaces an actionable notice instead of a raw upstream
    error. Non-retryable: retrying without config change cannot succeed.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.TOOL_NOT_CONFIGURED


class NonRetryableToolError(ToolError, ValueError):
    """A tool error that should never be retried.

    Also inherits ``ValueError`` so ``except ValueError`` handlers and the MCP
    retry logic treat it as non-retryable.
    """

    default_code: ClassVar[ErrorCode] = ErrorCode.TOOL_NON_RETRYABLE


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
