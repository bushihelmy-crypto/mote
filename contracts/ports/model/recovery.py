"""Read-only canonical ModelCall recovery query consumed by Session projection."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from mote.contracts.model.model_journal import ModelCallRecovery


class ModelRecoveryDisposition(StrEnum):
    RECOVERABLE = "recoverable"
    TERMINAL = "terminal"
    ABSENT = "absent"
    CORRUPT = "corrupt"
    UNSUPPORTED = "unsupported"
    LEGACY = "legacy"
    IDENTITY_MISMATCH = "identity_mismatch"
    IN_DOUBT = "in_doubt"


@dataclass(frozen=True, slots=True)
class ModelRecoveryInspection:
    model_call_id: str
    disposition: ModelRecoveryDisposition
    recovery: ModelCallRecovery | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.model_call_id:
            raise ValueError("Model recovery inspection requires a call identity")
        has_recovery = self.recovery is not None
        expects_recovery = self.disposition in {
            ModelRecoveryDisposition.RECOVERABLE,
            ModelRecoveryDisposition.TERMINAL,
            ModelRecoveryDisposition.IN_DOUBT,
        }
        if has_recovery != expects_recovery:
            raise ValueError("Model recovery disposition and evidence disagree")
        if self.recovery is not None and self.recovery.model_call_id != self.model_call_id:
            raise ValueError("Model recovery evidence identity mismatch")


class ModelCallRecoveryQuery(Protocol):
    def inspect_recovery(self, model_call_id: str) -> ModelRecoveryInspection: ...


__all__ = [
    "ModelCallRecoveryQuery",
    "ModelRecoveryDisposition",
    "ModelRecoveryInspection",
]
