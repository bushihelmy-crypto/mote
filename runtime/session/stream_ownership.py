"""Cross-process fenced ownership for one canonical Session stream writer."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from mote.contracts.ports.runtime.lease import LeaseCoordinator, LeaseEpoch
from mote.runtime.control.leases import FileLeaseCoordinator


class SessionStreamOwnership:
    def __init__(
        self,
        runtime_root: Path,
        session_id: str,
        *,
        coordinator: LeaseCoordinator | None = None,
        owner_id: str | None = None,
        ttl_seconds: float = 30.0,
    ) -> None:
        if not session_id or ttl_seconds <= 0:
            raise ValueError("Session stream ownership identity is invalid")
        self._coordinator = coordinator or FileLeaseCoordinator(Path(runtime_root) / "session-stream-leases.json")
        self._subject = f"session-stream:{session_id}"
        self._owner_id = owner_id or f"session-writer:{uuid4().hex}"
        self._ttl_seconds = ttl_seconds
        self._lease: LeaseEpoch | None = None

    @property
    def lifecycle_generation(self) -> int:
        if self._lease is None:
            raise RuntimeError("Session stream writer has not acquired ownership")
        return self._lease.fencing_token

    @contextmanager
    def guard(self) -> Iterator[None]:
        if self._lease is None:
            self._lease = self._coordinator.acquire(self._subject, self._owner_id, self._ttl_seconds)
        else:
            self._lease = self._coordinator.renew(self._lease, self._ttl_seconds)
        with self._coordinator.guard(self._subject, self._lease.fencing_token):
            yield

    def release(self) -> None:
        lease = self._lease
        if lease is None:
            return
        self._coordinator.release(lease)
        self._lease = None


__all__ = ["SessionStreamOwnership"]
