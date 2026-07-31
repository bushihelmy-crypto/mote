"""Static checker declarations; execution results are separate artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CheckerStatus(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"


@dataclass(frozen=True, slots=True)
class GateDeclaration:
    gate_id: str
    authority: str
    checker_id: str
    checker_status: CheckerStatus
    fixed_command: str | None
    declaration_owner: str
    final_hard_prerequisite: str
    evidence_schema: str = "gate-status-v1"

    def __post_init__(self) -> None:
        required = (
            self.gate_id,
            self.authority,
            self.checker_id,
            self.declaration_owner,
            self.final_hard_prerequisite,
            self.evidence_schema,
        )
        if any(not value for value in required):
            raise ValueError("gate declarations must be complete")
        if (self.checker_status is CheckerStatus.PRESENT) != (self.fixed_command is not None):
            raise ValueError("present checker status and fixed command must agree")


class GateEnforcement(StrEnum):
    UNAVAILABLE = "unavailable"
    REPORT = "report"
    RATCHET = "ratchet"
    HARD = "hard"


class GateResult(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    NOT_RUN = "not_run"


@dataclass(frozen=True, slots=True)
class GateStatusArtifact:
    gate_id: str
    checker_id: str
    checker_version: str
    executed_command: str
    source_digest: str
    declaration_digest: str
    executed_at: str
    checker_status: CheckerStatus
    enforcement: GateEnforcement
    result: GateResult
    violations: tuple[str, ...]
    evidence_path: str
    remediation_owner: str

    def __post_init__(self) -> None:
        required = (
            self.gate_id,
            self.checker_id,
            self.checker_version,
            self.executed_command,
            self.source_digest,
            self.declaration_digest,
            self.executed_at,
            self.evidence_path,
            self.remediation_owner,
        )
        if any(not value for value in required):
            raise ValueError("gate status artifacts must be complete")
        if not self.source_digest.startswith("sha256:") or not self.declaration_digest.startswith("sha256:"):
            raise ValueError("gate status digests must be sha256 identities")
        if self.result is GateResult.PASS and self.violations:
            raise ValueError("a passing gate status cannot contain violations")
        if self.result is GateResult.FAIL and not self.violations:
            raise ValueError("a failing gate status requires violations")


__all__ = [
    "CheckerStatus",
    "GateDeclaration",
    "GateEnforcement",
    "GateResult",
    "GateStatusArtifact",
]
