"""Storage-independent Runtime ownership and fencing."""

from __future__ import annotations

from typing import ContextManager, Protocol, runtime_checkable


@runtime_checkable
class LeaseEpoch(Protocol):
    @property
    def subject(self) -> str: ...

    @property
    def owner_id(self) -> str: ...

    @property
    def fencing_token(self) -> int: ...

    @property
    def expires_at(self) -> float: ...


@runtime_checkable
class LeaseCoordinator(Protocol):
    """Acquire, renew and fence ownership for a namespaced subject."""

    def acquire(self, subject: str, owner_id: str, ttl_seconds: float) -> LeaseEpoch: ...

    def renew(self, lease: LeaseEpoch, ttl_seconds: float) -> LeaseEpoch: ...

    def release(self, lease: LeaseEpoch) -> None: ...

    def assert_current(self, subject: str, fencing_token: int) -> None: ...

    def guard(self, subject: str, fencing_token: int) -> ContextManager[None]: ...

    def guard_many(self, bindings: tuple[tuple[str, int], ...]) -> ContextManager[None]: ...


__all__ = ["LeaseCoordinator", "LeaseEpoch"]
