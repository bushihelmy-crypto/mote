"""Typed Prompt policy data shared across composition boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

PromptPolicyDisposition = Literal[
    "allow",
    "deny",
    "redact",
    "enrich",
    "failed_open",
    "failed_closed",
]


@dataclass(frozen=True)
class PromptPolicyTraceEntry:
    """One sanitized PromptPolicy contribution in execution order."""

    step: str
    disposition: PromptPolicyDisposition
    detail: str = ""


@dataclass(frozen=True)
class PromptIntent:
    """A user prompt that has not crossed the history/model boundary."""

    prompt: str


@dataclass(frozen=True)
class PromptPolicyContribution:
    """A bounded extension contribution: enrich or monotonically deny."""

    allowed: bool = True
    additional_context: tuple[str, ...] = ()
    reason: str = ""

    @classmethod
    def enrich(cls, *context: str) -> "PromptPolicyContribution":
        return cls(additional_context=tuple(context))

    @classmethod
    def deny(cls, reason: str) -> "PromptPolicyContribution":
        return cls(allowed=False, reason=reason)


@dataclass(frozen=True)
class PromptDecision:
    """The safe prompt view and final admission result consumed by Role."""

    accepted: bool
    prompt: str
    additional_context: tuple[str, ...] = ()
    reason: str = ""
    terminate: bool = False
    trace: tuple[PromptPolicyTraceEntry, ...] = ()

    @classmethod
    def accept(
        cls,
        prompt: str,
        *,
        additional_context: tuple[str, ...] = (),
        trace: tuple[PromptPolicyTraceEntry, ...] = (),
    ) -> "PromptDecision":
        return cls(
            accepted=True,
            prompt=prompt,
            additional_context=additional_context,
            trace=trace,
        )

    @classmethod
    def reject(
        cls,
        prompt: str,
        reason: str,
        *,
        additional_context: tuple[str, ...] = (),
        terminate: bool = False,
        trace: tuple[PromptPolicyTraceEntry, ...] = (),
    ) -> "PromptDecision":
        return cls(
            accepted=False,
            prompt=prompt,
            additional_context=additional_context,
            reason=reason,
            terminate=terminate,
            trace=trace,
        )


__all__ = [
    "PromptDecision",
    "PromptIntent",
    "PromptPolicyContribution",
    "PromptPolicyDisposition",
    "PromptPolicyTraceEntry",
]
