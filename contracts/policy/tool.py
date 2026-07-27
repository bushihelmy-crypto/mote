"""Typed ToolCall policy data.

An intent describes a call that has not run.  A decision is the only value the
tool execution pipeline accepts before crossing the invocation boundary.  These
records are pure data and deliberately know nothing about hooks, permission
engines, tools, or the runtime event transport.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Optional

ToolPolicyDisposition = Literal[
    "allow",
    "deny",
    "rewrite",
    "redact",
    "enrich",
    "stop",
    "failed_open",
    "failed_closed",
]


@dataclass(frozen=True)
class ToolPolicyTraceEntry:
    """One sanitized policy contribution in deterministic execution order."""

    step: str
    disposition: ToolPolicyDisposition
    detail: str = ""
    rewritten_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolCallInspection:
    """A deny-only extension verdict for one tool call.

    The extension slot deliberately has no force-allow or rewrite result.  An
    extension may narrow framework behavior by denying a call, but core
    permission and sandbox policy remain authoritative.
    """

    allowed: bool = True
    reason: str = ""

    @classmethod
    def allow(cls) -> "ToolCallInspection":
        return cls(allowed=True)

    @classmethod
    def deny(cls, reason: str) -> "ToolCallInspection":
        return cls(allowed=False, reason=reason)


@dataclass(frozen=True)
class ToolCallIntent:
    """A tool call awaiting policy evaluation."""

    tool_name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    tool_call_id: Optional[str] = None
    scope: tuple = ()


@dataclass(frozen=True)
class ToolCallDecision:
    """Final authorization result consumed by the tool invocation boundary."""

    allowed: bool
    arguments: Mapping[str, Any] = field(default_factory=dict)
    reason: str = ""
    terminate: bool = False
    trace: tuple[ToolPolicyTraceEntry, ...] = ()

    @classmethod
    def allow(
        cls,
        arguments: Mapping[str, Any],
        *,
        trace: tuple[ToolPolicyTraceEntry, ...] = (),
    ) -> "ToolCallDecision":
        return cls(allowed=True, arguments=dict(arguments), trace=trace)

    @classmethod
    def deny(
        cls,
        arguments: Mapping[str, Any],
        reason: str,
        *,
        terminate: bool = False,
        trace: tuple[ToolPolicyTraceEntry, ...] = (),
    ) -> "ToolCallDecision":
        return cls(
            allowed=False,
            arguments=dict(arguments),
            reason=reason,
            terminate=terminate,
            trace=trace,
        )


@dataclass(frozen=True)
class ToolResultIntent:
    """Trusted raw execution result entering settlement presentation policy."""

    tool_name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    output: str = ""
    execution_success: bool = True
    executed: bool = True
    error: Optional[Mapping[str, Any]] = None
    tool_call_id: Optional[str] = None
    is_readonly: bool = False
    scope: tuple = ()


@dataclass(frozen=True)
class ToolResultPresentation:
    """Safe model/UI representation; never rewrites execution truth."""

    output: str
    terminate: bool = False
    trace: tuple[ToolPolicyTraceEntry, ...] = ()


__all__ = [
    "ToolCallDecision",
    "ToolCallInspection",
    "ToolCallIntent",
    "ToolResultIntent",
    "ToolResultPresentation",
    "ToolPolicyDisposition",
    "ToolPolicyTraceEntry",
]
