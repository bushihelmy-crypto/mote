"""Contracts for handing an interactive Runtime surface to a human."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from mote.contracts.runtime import RuntimeRef
from mote.contracts.surface import SurfaceDescriptor


class HandoffStatus(StrEnum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class HandoffRequest:
    runtime_ref: RuntimeRef
    mode: str = "exclusive"
    message: str = ""
    selection: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class DriverHandoffHandle:
    handle_id: str
    surface: SurfaceDescriptor


@dataclass(frozen=True, slots=True)
class HumanHandoffOutcome:
    status: HandoffStatus
    human_message: str = ""
    detail: str = ""


@dataclass(frozen=True, slots=True)
class DriverHandoffResult:
    summary: str = ""
    resume_hint: str = ""


@dataclass(frozen=True, slots=True)
class HandoffOutcome:
    status: HandoffStatus
    runtime_ref: RuntimeRef
    from_revision: int
    to_revision: int
    human_message: str = ""
    detail: str = ""
    summary: str = ""
    resume_hint: str = ""


__all__ = [
    "DriverHandoffHandle",
    "DriverHandoffResult",
    "HandoffOutcome",
    "HandoffRequest",
    "HandoffStatus",
    "HumanHandoffOutcome",
]
