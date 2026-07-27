"""Typed data for context compaction policy decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CompactionProfile = Literal["balanced", "preserve", "emergency"]


@dataclass(frozen=True)
class CompactionIntent:
    trigger: str
    target_tokens: int
    urgency: Literal["soft", "hard"]
    custom_instructions: str = ""


@dataclass(frozen=True)
class CompactionPolicyContribution:
    """An extension may enrich instructions or select a safer profile."""

    additional_instructions: tuple[str, ...] = ()
    profile: Literal["balanced", "preserve"] | None = None


@dataclass(frozen=True)
class CompactionPolicyTraceEntry:
    step: str
    disposition: str
    detail: str = ""


@dataclass(frozen=True)
class CompactionDecision:
    profile: CompactionProfile
    custom_instructions: str = ""
    allow_destructive: bool = False
    trace: tuple[CompactionPolicyTraceEntry, ...] = ()


__all__ = [
    "CompactionDecision",
    "CompactionIntent",
    "CompactionPolicyContribution",
    "CompactionPolicyTraceEntry",
    "CompactionProfile",
]
