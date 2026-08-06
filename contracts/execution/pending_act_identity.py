"""Stable PendingAct identity shared across Contracts bounded contexts."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PendingActFrontierId:
    value: str

    def __post_init__(self) -> None:
        if type(self.value) is not str or not self.value:
            raise ValueError("PendingActFrontierId must be a non-empty string")


__all__ = ["PendingActFrontierId"]
