"""Canonical, presentation-ready error envelope + a single LLM-facing renderer.

This module is the missing *presentation contract* that ties the (previously
unconsumed) :meth:`MetaGPTError.to_dict` serialization to every boundary that
surfaces a failure to the model — tool results, background-task notifications,
task attachments. The principle is **one typed error contract, rendered (not
re-derived) at every presentation boundary**:

- :meth:`ErrorReport.from_exception` *normalizes* any ``BaseException`` into a
  uniform record (generalizing the philosophy of ``handlers.classify_llm_error``
  to be domain-agnostic): a typed :class:`MetaGPTError` contributes its
  ``code`` / ``retryable`` / ``recovery`` / structured :meth:`~MetaGPTError.detail`,
  while an un-typed exception degrades gracefully to an ``UNKNOWN`` record.
- :func:`render_error_block` is the **single** renderer that turns a report into
  the ``<error …>`` block the LLM sees, so tool and graph failures look
  identical regardless of which executor surface produced them.

``ErrorReport`` is a dependency-free frozen dataclass so this stays a leaf of the
exception package. Pydantic message objects (which are serialized into the
session rollout JSONL) store the JSON-native :meth:`~ErrorReport.as_dict` form;
plain dataclass result objects (``ToolResult`` / ``TaskAttachment``) hold the
``ErrorReport`` instance directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from metagpt.common.exception.base import MetaGPTError
from metagpt.common.exception.codes import ErrorCode, RecoveryAction
from metagpt.common.exception.handlers import is_retryable


@dataclass(frozen=True)
class ErrorReport:
    """Presentation-ready, serializable snapshot of a failure.

    Mirrors :meth:`MetaGPTError.to_dict` but is produced for *any* exception via
    :meth:`from_exception`, so callers never branch on the concrete error type.
    """

    error: str  # exception class name
    code: str  # ErrorCode value
    message: str
    retryable: bool
    recovery: str  # RecoveryAction value
    detail: dict[str, Any] = field(default_factory=dict)
    cause: str | None = None

    @classmethod
    def from_exception(cls, exc: BaseException) -> "ErrorReport":
        """Normalize any exception into an :class:`ErrorReport`.

        A typed :class:`MetaGPTError` contributes its full contract (stable
        ``code``, ``retryable`` marker, ``recovery`` hint, structured
        :meth:`~MetaGPTError.detail`). An un-typed exception degrades to an
        ``UNKNOWN`` record whose retry classification reuses the single source of
        truth, :func:`~metagpt.common.exception.handlers.is_retryable` — imported
        lazily so this module stays a leaf (importing it never pulls in the
        heavyweight ``handlers`` / ``common.utils`` chain).
        """
        if isinstance(exc, MetaGPTError):
            return cls(
                error=type(exc).__name__,
                code=exc.code.value,
                message=exc.message or type(exc).__name__,
                retryable=exc.retryable,
                recovery=exc.recovery.value,
                detail=dict(exc.detail()),
                cause=repr(exc.cause) if exc.cause is not None else None,
            )


        retryable = is_retryable(exc)
        recovery = RecoveryAction.RETRY if retryable else RecoveryAction.ABORT
        return cls(
            error=type(exc).__name__,
            code=ErrorCode.UNKNOWN.value,
            message=str(exc) or type(exc).__name__,
            retryable=retryable,
            recovery=recovery.value,
            detail={"type": type(exc).__name__},
            cause=None,
        )

    def as_dict(self) -> dict[str, Any]:
        """JSON-native form for embedding on serialized (pydantic) messages."""
        return {
            "error": self.error,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "recovery": self.recovery,
            "detail": self.detail,
            "cause": self.cause,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ErrorReport":
        """Rebuild a report from its :meth:`as_dict` form (e.g. off a message)."""
        return cls(
            error=data.get("error", ""),
            code=data.get("code", ErrorCode.UNKNOWN.value),
            message=data.get("message", ""),
            retryable=bool(data.get("retryable", False)),
            recovery=data.get("recovery", RecoveryAction.ABORT.value),
            detail=dict(data.get("detail") or {}),
            cause=data.get("cause"),
        )


def _render_detail(detail: dict[str, Any], indent: str = "  ") -> list[str]:
    """Render the structured ``detail`` dict as indented ``key: value`` lines.

    A ``failures`` list (graph batch failure) is expanded into per-node lines so
    the model sees every failed node's code + message, not an opaque blob.
    """
    lines: list[str] = []
    for key, value in detail.items():
        if key == "type":
            continue  # redundant with the rendered class name
        if key == "failures" and isinstance(value, list):
            lines.append(f"{indent}failed nodes:")
            for item in value:
                if isinstance(item, dict):
                    node = item.get("node", "?")
                    code = item.get("code", "")
                    msg = item.get("message", "")
                    suffix = f" [{code}]" if code and code != ErrorCode.UNKNOWN.value else ""
                    lines.append(f"{indent}  - {node}{suffix}: {msg}")
                else:
                    lines.append(f"{indent}  - {item}")
            continue
        lines.append(f"{indent}{key}: {value}")
    return lines


def render_error_block(report: ErrorReport) -> str:
    """Render an :class:`ErrorReport` as the uniform ``<error …>`` block.

    The single LLM-facing error format shared by every executor surface (tool
    results, task notifications, task attachments). The opening tag carries the
    machine-readable ``code`` / ``recovery`` / ``retryable`` so the model can
    reason about *how* to react, not just *what* failed.
    """
    head = (
        f'<error code="{report.code}" recovery="{report.recovery}" '
        f'retryable="{str(report.retryable).lower()}">'
    )
    lines = [head, report.message]
    lines.extend(_render_detail(report.detail))
    if report.cause:
        lines.append(f"  cause: {report.cause}")
    lines.append("</error>")
    return "\n".join(lines)
