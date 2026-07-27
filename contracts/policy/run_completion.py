"""Typed data for post-flow run completion decisions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RunCompletionIntent:
    output_committed: bool
    background_pending: bool
    remaining_continuations: int


@dataclass(frozen=True)
class RunCompletionPolicyContribution:
    """A bounded extension may request or deny another turn and add context."""

    request_continuation: bool = False
    deny_continuation: bool = False
    additional_context: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class RunCompletionPolicyTraceEntry:
    step: str
    disposition: str
    detail: str = ""


@dataclass(frozen=True)
class RunCompletionDecision:
    continue_run: bool
    additional_context: tuple[str, ...] = ()
    reason: str = ""
    trace: tuple[RunCompletionPolicyTraceEntry, ...] = ()


__all__ = [
    "RunCompletionDecision",
    "RunCompletionIntent",
    "RunCompletionPolicyContribution",
    "RunCompletionPolicyTraceEntry",
]
