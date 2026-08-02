"""Runtime adapter from operation semantics to the canonical lease mechanism."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from mote.contracts.ports.runtime.lease import LeaseCoordinator
from mote.contracts.runtime.lease import RuntimeLease
from mote.contracts.runtime.operation_ownership import OperationBackend, OperationOwnership, OperationOwnershipRequest


class LeaseOperationOwnership:
    def __init__(self, coordinator: LeaseCoordinator, *, backend: OperationBackend) -> None:
        self._coordinator = coordinator
        self._backend = backend

    @staticmethod
    def _subject(request: OperationOwnershipRequest) -> str:
        return f"operation:{request.deployment_id}:{request.operation_id}"

    def claim(self, request: OperationOwnershipRequest, ttl_seconds: float) -> OperationOwnership:
        if request.backend is not self._backend:
            raise ValueError("operation backend does not match the activated adapter")
        lease = self._coordinator.acquire(self._subject(request), request.holder_id, ttl_seconds)
        return self._project(request, lease)

    def renew(self, ownership: OperationOwnership, ttl_seconds: float) -> OperationOwnership:
        lease = self._coordinator.renew(self._lease(ownership), ttl_seconds)
        return self._project(ownership.request, lease)

    def assert_current(self, ownership: OperationOwnership) -> None:
        self._coordinator.assert_current(ownership.subject, ownership.fencing_token)

    @contextmanager
    def guard(self, ownership: OperationOwnership) -> Iterator[None]:
        with self._coordinator.guard(ownership.subject, ownership.fencing_token):
            yield

    @contextmanager
    def guard_many(self, ownerships: tuple[OperationOwnership, ...]) -> Iterator[None]:
        if not ownerships:
            raise ValueError("operation ownership guard set must not be empty")
        with self._coordinator.guard_many(
            tuple((ownership.subject, ownership.fencing_token) for ownership in ownerships)
        ):
            yield

    def release(self, ownership: OperationOwnership) -> None:
        self._coordinator.release(self._lease(ownership))

    @staticmethod
    def _lease(ownership: OperationOwnership) -> RuntimeLease:
        return RuntimeLease(
            ownership.subject,
            ownership.request.holder_id,
            ownership.fencing_token,
            ownership.expires_at,
        )

    @staticmethod
    def _project(request: OperationOwnershipRequest, lease) -> OperationOwnership:
        return OperationOwnership(request, lease.subject, lease.fencing_token, lease.expires_at)


__all__ = ["LeaseOperationOwnership"]
