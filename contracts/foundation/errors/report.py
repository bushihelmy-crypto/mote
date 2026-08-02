"""Canonical error report envelope and renderer.

This module is the missing *presentation contract* that ties the (previously
unconsumed) :meth:`MoteError.to_dict` serialization to every boundary that
surfaces a failure to the model — tool results, background-task notifications,
task attachments. The principle is **one typed error contract, rendered (not
re-derived) at every presentation boundary**:

- :meth:`ErrorReport.from_exception` *normalizes* any ``BaseException`` into a
  uniform record (generalizing the philosophy of ``handlers.classify_llm_error``
  to be domain-agnostic): a typed :class:`MoteError` contributes its
  ``code`` / ``retryable`` / ``recovery`` / structured :meth:`~MoteError.detail`,
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

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping, cast

from mote.contracts.events.envelope import JsonValue, freeze_json, thaw_json
from mote.contracts.foundation.errors.base import MoteError
from mote.contracts.foundation.errors.codes import ErrorCode, RecoveryAction

ERROR_REPORT_SCHEMA = "mote.error-report/v1"


class ErrorNamespace(StrEnum):
    FOUNDATION = "foundation"
    MODEL = "model"
    SERVICE = "service"
    TOOL = "tool"
    FILE = "file"
    MEDIA = "media"
    WORKFLOW = "workflow"
    BACKGROUND_TASK = "background_task"
    OAUTH = "oauth"
    CONFIG = "config"
    AGENT = "agent"
    OUTPUT = "output"
    RUNTIME = "runtime"
    ARTIFACT = "artifact"
    RESOURCE = "resource"


def _namespace_for(code: ErrorCode) -> ErrorNamespace:
    value = code.value
    prefixes = (
        ("LLM_", ErrorNamespace.MODEL),
        ("MODEL_", ErrorNamespace.MODEL),
        ("SERVICE_", ErrorNamespace.SERVICE),
        ("TOOL", ErrorNamespace.TOOL),
        ("FILE_", ErrorNamespace.FILE),
        ("MEDIA_", ErrorNamespace.MEDIA),
        ("GRAPH_", ErrorNamespace.WORKFLOW),
        ("BG_TASK_", ErrorNamespace.BACKGROUND_TASK),
        ("OAUTH", ErrorNamespace.OAUTH),
        ("CONFIG_", ErrorNamespace.CONFIG),
        ("ENV_", ErrorNamespace.CONFIG),
        ("AGENT_", ErrorNamespace.AGENT),
        ("SESSION_", ErrorNamespace.AGENT),
        ("OUTPUT_", ErrorNamespace.OUTPUT),
        ("RUN_", ErrorNamespace.OUTPUT),
        ("RUNTIME_", ErrorNamespace.RUNTIME),
        ("LEASE_", ErrorNamespace.RUNTIME),
        ("ARTIFACT_", ErrorNamespace.ARTIFACT),
        ("RESOURCE_", ErrorNamespace.RESOURCE),
    )
    return next((namespace for prefix, namespace in prefixes if value.startswith(prefix)), ErrorNamespace.FOUNDATION)


@dataclass(frozen=True)
class ErrorReport:
    """Presentation-ready, serializable snapshot of a failure.

    Mirrors :meth:`MoteError.to_dict` but is produced for *any* exception via
    :meth:`from_exception`, so callers never branch on the concrete error type.
    """

    error: str  # exception class name
    code: str  # ErrorCode value
    message: str
    retryable: bool
    recovery: str  # RecoveryAction value
    detail: Mapping[str, JsonValue] = field(default_factory=dict)
    cause: str | None = None
    namespace: ErrorNamespace | None = None

    def __post_init__(self) -> None:
        for name in ("error", "code", "message", "recovery"):
            if type(getattr(self, name)) is not str:
                raise TypeError(f"error report {name} must be a string")
        if type(self.retryable) is not bool:
            raise TypeError("error report retryable must be a boolean")
        if self.cause is not None and type(self.cause) is not str:
            raise TypeError("error report cause must be a string or null")
        if self.namespace is not None and not isinstance(self.namespace, ErrorNamespace):
            raise TypeError("error report namespace must be an ErrorNamespace or null")
        try:
            code = ErrorCode(self.code)
            RecoveryAction(self.recovery)
        except ValueError as exc:
            raise ValueError("error report code or recovery is unknown") from exc
        expected = _namespace_for(code)
        if self.namespace is not None and self.namespace is not expected:
            raise ValueError("error report namespace does not own code")
        object.__setattr__(self, "namespace", expected)
        frozen = freeze_json(self.detail, path="error_report.context")
        if not isinstance(frozen, Mapping):
            raise ValueError("error report context must be an object")
        object.__setattr__(self, "detail", frozen)

    @classmethod
    def from_exception(cls, exc: BaseException) -> "ErrorReport":
        """Normalize any exception into an :class:`ErrorReport`.

        A typed :class:`MoteError` contributes its full contract (stable
        ``code``, ``retryable`` marker, ``recovery`` hint, structured
        :meth:`~MoteError.detail`). An un-typed exception degrades to an
        ``UNKNOWN`` record whose retry classification reuses the single source of
        truth, :func:`~mote.runtime.resilience.error_classification.is_retryable` — imported
        lazily so this module stays a leaf (importing it never pulls in the
        heavyweight Runtime classification chain).
        """
        if isinstance(exc, MoteError):
            return cls(
                error=type(exc).__name__,
                code=exc.code.value,
                message=exc.message or type(exc).__name__,
                retryable=exc.retryable,
                recovery=exc.recovery.value,
                detail=cast(Mapping[str, JsonValue], freeze_json(exc.detail(), path="error_report.context")),
                cause=repr(exc.cause) if exc.cause is not None else None,
            )

        retryable = isinstance(exc, (ConnectionError, TimeoutError, json.JSONDecodeError))
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

    def as_dict(self) -> dict[str, object]:
        """JSON-native form for embedding on serialized (pydantic) messages."""
        namespace = self.namespace
        assert namespace is not None
        return {
            "schema": ERROR_REPORT_SCHEMA,
            "namespace": namespace.value,
            "error": self.error,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "recovery": self.recovery,
            "context": thaw_json(cast(JsonValue, self.detail)),
            "cause": self.cause,
        }

    @classmethod
    def from_dict(cls, data: object) -> "ErrorReport":
        """Strictly decode the sole current durable envelope; legacy fails closed."""
        keys = {"schema", "namespace", "error", "code", "message", "retryable", "recovery", "context", "cause"}
        if type(data) is not dict or set(data) != keys:
            raise ValueError("error report envelope shape is invalid")
        raw = cast(dict[str, object], data)
        if raw["schema"] != ERROR_REPORT_SCHEMA:
            raise ValueError("unsupported error report schema")
        strings = ("error", "code", "message", "recovery")
        if any(type(raw[name]) is not str for name in strings):
            raise ValueError("error report string field is invalid")
        if type(raw["retryable"]) is not bool:
            raise ValueError("error report retryable is invalid")
        cause = raw["cause"]
        if cause is not None and type(cause) is not str:
            raise ValueError("error report cause is invalid")
        try:
            namespace = ErrorNamespace(cast(str, raw["namespace"]))
        except (TypeError, ValueError) as exc:
            raise ValueError("error report namespace is unknown") from exc
        context = freeze_json(raw["context"], path="error_report.context")
        if not isinstance(context, Mapping):
            raise ValueError("error report context must be an object")
        return cls(
            error=cast(str, raw["error"]),
            code=cast(str, raw["code"]),
            message=cast(str, raw["message"]),
            retryable=cast(bool, raw["retryable"]),
            recovery=cast(str, raw["recovery"]),
            detail=context,
            cause=cast(str | None, cause),
            namespace=namespace,
        )


def _render_detail(detail: Mapping[str, JsonValue], indent: str = "  ") -> list[str]:
    """Render the structured ``detail`` dict as indented ``key: value`` lines.

    A ``failures`` list (graph batch failure) is expanded into per-node lines so
    the model sees every failed node's code + message, not an opaque blob.
    """
    lines: list[str] = []
    thawed = thaw_json(cast(JsonValue, detail))
    assert isinstance(thawed, dict)
    for key, value in thawed.items():
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
    head = f'<error code="{report.code}" recovery="{report.recovery}" ' f'retryable="{str(report.retryable).lower()}">'
    lines = [head, report.message]
    lines.extend(_render_detail(report.detail))
    if report.cause:
        lines.append(f"  cause: {report.cause}")
    lines.append("</error>")
    return "\n".join(lines)
