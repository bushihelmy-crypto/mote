"""Cross-process fenced ownership for workspace maintenance."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from pathlib import Path
from threading import RLock
from typing import AsyncIterator, Callable
from uuid import uuid4

from mote.contracts.runtime.errors import LeaseUnavailableError
from mote.contracts.runtime.lease import RuntimeLease
from mote.runtime.control.leases import FileLeaseCoordinator


class WorkspaceCleanupGate:
    """Own cleanup/GC with one durable, monotonic workspace lease."""

    def __init__(
        self,
        *,
        owner_id: str | None = None,
        ttl_seconds: float = 3600.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._owner_id = owner_id or f"workspace-maintenance:{uuid4().hex}"
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._lock = RLock()
        self._claims: dict[str, tuple[FileLeaseCoordinator, RuntimeLease]] = {}

    def _coordinator(self, key: str) -> FileLeaseCoordinator:
        root = Path(key).expanduser().resolve(strict=False)
        return FileLeaseCoordinator(root / ".maintenance" / "leases.json", clock=self._clock)

    @staticmethod
    def _subject(key: str) -> str:
        return f"workspace-maintenance:{Path(key).expanduser().resolve(strict=False)}"

    def try_acquire(self, key: str) -> bool:
        with self._lock:
            if key in self._claims:
                return False
            coordinator = self._coordinator(key)
            try:
                lease = coordinator.acquire(self._subject(key), self._owner_id, self._ttl_seconds)
            except LeaseUnavailableError:
                return False
            self._claims[key] = (coordinator, lease)
            return True

    def assert_current(self, key: str) -> None:
        with self._lock:
            claim = self._claims.get(key)
            if claim is None:
                raise RuntimeError("workspace maintenance ownership is absent")
            claim[0].assert_current(claim[1].subject, claim[1].fencing_token)

    def renew(self, key: str) -> None:
        with self._lock:
            claim = self._claims.get(key)
            if claim is None:
                raise RuntimeError("workspace maintenance ownership is absent")
            self._claims[key] = (
                claim[0],
                claim[0].renew(claim[1], self._ttl_seconds),
            )

    def release(self, key: str) -> None:
        with self._lock:
            claim = self._claims.pop(key, None)
            if claim is None:
                return
            claim[0].release(claim[1])

    @asynccontextmanager
    async def claim(self, key: str) -> AsyncIterator[bool]:
        acquired = self.try_acquire(key)
        try:
            yield acquired
        finally:
            if acquired:
                self.release(key)


__all__ = ["WorkspaceCleanupGate"]
