"""Shared query result vocabulary for async-work consumers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from mote.contracts.async_work.observation import AsyncWorkObservation


class AsyncWorkQueryDisposition(str, Enum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    OWNER_LOST = "owner_lost"
    INCARNATION_LOST = "incarnation_lost"
    DEFINITION_MISMATCH = "definition_mismatch"
    PRINCIPAL_MISMATCH = "principal_mismatch"
    CONTROL_UNAVAILABLE = "control_unavailable"


@dataclass(frozen=True, slots=True)
class AsyncWorkQueryResult:
    disposition: AsyncWorkQueryDisposition
    observation: AsyncWorkObservation | None

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, AsyncWorkQueryDisposition):
            raise TypeError("async-work query disposition is invalid")
        found = self.disposition is AsyncWorkQueryDisposition.FOUND
        if found != (self.observation is not None):
            raise ValueError("async-work observation is present exactly for FOUND")


__all__ = ["AsyncWorkQueryDisposition", "AsyncWorkQueryResult"]
