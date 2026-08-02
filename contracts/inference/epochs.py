"""Atomic epoch snapshot used to fence inference wire authorization."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ExecutionEpochSnapshot:
    backup_epoch: int
    admission_epoch: int

    def __post_init__(self) -> None:
        if self.backup_epoch < 1 or self.admission_epoch < 1:
            raise ValueError("execution epochs must be positive")

    def pair(self) -> tuple[int, int]:
        return self.backup_epoch, self.admission_epoch


class ExecutionEpochSource(Protocol):
    def snapshot(self) -> ExecutionEpochSnapshot: ...


__all__ = ["ExecutionEpochSnapshot", "ExecutionEpochSource"]
