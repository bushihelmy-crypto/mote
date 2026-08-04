"""Canonical Workflow effect admission contract."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class WorkflowEffectAdmissionDisposition(str, Enum):
    ACCEPTED = "accepted"
    IDEMPOTENT = "idempotent"
    BACKPRESSURED = "backpressured"


@dataclass(frozen=True, slots=True)
class WorkflowEffectAdmissionReceipt:
    effect_id: str
    disposition: WorkflowEffectAdmissionDisposition
    revision: int | None


class WorkflowEffectCapacityError(RuntimeError):
    """Raised only when a new effect cannot be durably admitted."""


__all__ = [
    "WorkflowEffectAdmissionDisposition",
    "WorkflowEffectAdmissionReceipt",
    "WorkflowEffectCapacityError",
]
