"""Typed ToolCall policy data.

An intent describes a call that has not run.  A decision is the only value the
tool execution pipeline accepts before crossing the invocation boundary.  These
records are pure data and deliberately know nothing about hooks, permission
engines, tools, or the runtime event transport.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Mapping, Optional

from mote.contracts.events.envelope import JsonValue
from mote.contracts.tool.arguments import ToolArguments, freeze_tool_arguments
from mote.contracts.tool.identity import ToolInvocationIdentity

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

    identity: ToolInvocationIdentity
    tool_name: str
    arguments: ToolArguments = field(default_factory=dict)
    scope: tuple = ()


@dataclass(frozen=True)
class ToolCallDecision:
    """Final authorization result consumed by the tool invocation boundary."""

    identity: ToolInvocationIdentity
    allowed: bool
    arguments: ToolArguments = field(default_factory=dict)
    reason: str = ""
    terminate: bool = False
    approval_required: bool = False
    permission_targets: tuple[str, ...] = ()
    mutates_fs: bool = False
    trace: tuple[ToolPolicyTraceEntry, ...] = ()

    @classmethod
    def allow(
        cls,
        identity: ToolInvocationIdentity,
        arguments: ToolArguments,
        *,
        trace: tuple[ToolPolicyTraceEntry, ...] = (),
    ) -> "ToolCallDecision":
        return cls(identity=identity, allowed=True, arguments=freeze_tool_arguments(arguments), trace=trace)

    @classmethod
    def deny(
        cls,
        identity: ToolInvocationIdentity,
        arguments: ToolArguments,
        reason: str,
        *,
        terminate: bool = False,
        trace: tuple[ToolPolicyTraceEntry, ...] = (),
    ) -> "ToolCallDecision":
        return cls(
            identity=identity,
            allowed=False,
            arguments=freeze_tool_arguments(arguments),
            reason=reason,
            terminate=terminate,
            trace=trace,
        )

    @classmethod
    def require_approval(
        cls,
        identity: ToolInvocationIdentity,
        arguments: ToolArguments,
        *,
        reason: str,
        permission_targets: tuple[str, ...],
        mutates_fs: bool,
        trace: tuple[ToolPolicyTraceEntry, ...] = (),
    ) -> "ToolCallDecision":
        return cls(
            identity=identity,
            allowed=False,
            arguments=freeze_tool_arguments(arguments),
            reason=reason,
            approval_required=True,
            permission_targets=permission_targets,
            mutates_fs=mutates_fs,
            trace=trace,
        )


@dataclass(frozen=True)
class ToolResultIntent:
    """Trusted raw execution result entering settlement presentation policy."""

    identity: ToolInvocationIdentity
    tool_name: str
    arguments: ToolArguments = field(default_factory=dict)
    output: str = ""
    execution_success: bool = True
    executed: bool = True
    error: Optional[Mapping[str, JsonValue]] = None
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
