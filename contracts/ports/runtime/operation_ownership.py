"""Consumer-owned Port for fenced durable operation ownership."""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Protocol

from mote.contracts.runtime.operation_ownership import OperationOwnership, OperationOwnershipRequest


class OperationOwnershipPort(Protocol):
    def claim(self, request: OperationOwnershipRequest, ttl_seconds: float) -> OperationOwnership: ...
    def renew(self, ownership: OperationOwnership, ttl_seconds: float) -> OperationOwnership: ...
    def assert_current(self, ownership: OperationOwnership) -> None: ...
    def guard(self, ownership: OperationOwnership) -> AbstractContextManager[None]: ...
    def guard_many(self, ownerships: tuple[OperationOwnership, ...]) -> AbstractContextManager[None]: ...
    def release(self, ownership: OperationOwnership) -> None: ...


__all__ = ["OperationOwnershipPort"]
