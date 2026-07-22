"""Leaf interfaces for replaceable distributed run coordination."""
from __future__ import annotations

from typing import ContextManager, Protocol, runtime_checkable


@runtime_checkable
class LeaseEpoch(Protocol):
    @property
    def run_id(self) -> str:
        ...

    @property
    def owner_id(self) -> str:
        ...

    @property
    def fencing_token(self) -> int:
        ...

    @property
    def expires_at(self) -> float:
        ...


@runtime_checkable
class RunLeaseCoordinator(Protocol):
    """Storage-independent lease and fencing operations."""

    def acquire(self, run_id: str, owner_id: str, ttl_seconds: float) -> LeaseEpoch:
        ...

    def renew(self, lease: LeaseEpoch, ttl_seconds: float) -> LeaseEpoch:
        ...

    def release(self, lease: LeaseEpoch) -> None:
        ...

    def assert_current(self, run_id: str, fencing_token: int) -> None:
        ...

    def guard(self, run_id: str, fencing_token: int) -> ContextManager[None]:
        ...


__all__ = ["LeaseEpoch", "RunLeaseCoordinator"]
