"""Durable Graph continuation independent of PendingAct lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from mote.contracts.execution.pending_act_identity import PendingActFrontierId


class RecoveryTarget(StrEnum):
    ACT = "act"
    OBSERVE = "observe"


@dataclass(frozen=True, slots=True)
class RunRecoveryCursor:
    run_id: str
    revision: int
    next_node: RecoveryTarget
    pending_act_id: PendingActFrontierId | None
    continue_inference: bool

    def __post_init__(self) -> None:
        if type(self.run_id) is not str or not self.run_id:
            raise ValueError("run cursor run_id must be a non-empty string")
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("run cursor revision must be non-negative")
        if not isinstance(self.next_node, RecoveryTarget):
            raise TypeError("run cursor next_node must be RecoveryTarget")
        if self.pending_act_id is not None and not isinstance(self.pending_act_id, PendingActFrontierId):
            raise TypeError("run cursor pending_act_id has the wrong type")
        if type(self.continue_inference) is not bool:
            raise TypeError("run cursor continue_inference must be bool")
        if self.next_node is RecoveryTarget.ACT and self.pending_act_id is None:
            raise ValueError("ACT cursor must identify its active PendingAct")


__all__ = ["RecoveryTarget", "RunRecoveryCursor"]
