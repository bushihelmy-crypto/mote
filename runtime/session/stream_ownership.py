"""Cross-process fenced ownership for one canonical Session stream writer."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from mote.contracts.ports.runtime.lease import LeaseCoordinator, LeaseEpoch
from mote.contracts.runtime.errors import LeaseCoordinatorUnavailableError, LeaseFencedError
from mote.contracts.runtime.lease import RuntimeLeasePolicy
from mote.runtime.control.leases import FileLeaseCoordinator, LeaseHandle
from mote.runtime.persistence.async_io import run_disk_io


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
        self._handle: LeaseHandle | None = None

    @property
    def lifecycle_generation(self) -> int:
        if self._lease is None:
            raise RuntimeError("Session stream writer has not acquired ownership")
        return self._lease.fencing_token

    @contextmanager
    def guard(self) -> Iterator[None]:
        if self._lease is None:
            self._lease = self._coordinator.acquire(self._subject, self._owner_id, self._ttl_seconds)
        elif self._handle is None:
            self._lease = self._coordinator.renew(self._lease, self._ttl_seconds)
        self._adopt_handle_if_running()
        guard = (
            self._handle.guard()
            if self._handle is not None
            else self._coordinator.guard(self._subject, self._lease.fencing_token)
        )
        with guard:
            yield

    async def start(self) -> None:
        """Acquire ownership and start its heartbeat on the owner event loop."""
        if self._handle is not None:
            return
        if self._lease is None:
            self._lease = await run_disk_io(
                self._coordinator.acquire,
                self._subject,
                self._owner_id,
                self._ttl_seconds,
            )
        self._adopt_handle_if_running()
        if self._handle is None:
            raise RuntimeError("Session stream ownership requires a running event loop")

    def _adopt_handle_if_running(self) -> None:
        if self._handle is not None or self._lease is None:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        interval = min(self._ttl_seconds / 3, self._ttl_seconds - 0.001)
        self._handle = LeaseHandle(
            self._coordinator,
            subject=self._subject,
            owner_id=self._owner_id,
            policy=RuntimeLeasePolicy(
                ttl_seconds=self._ttl_seconds,
                renew_interval_seconds=interval,
            ),
        ).adopt_nowait(self._lease)

    async def aclose(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            await handle.close()
            self._lease = None
            return
        self.release()

    def release(self) -> None:
        lease = self._lease
        if lease is None:
            return
        try:
            self._coordinator.release(lease)
        except (LeaseFencedError, LeaseCoordinatorUnavailableError):
            pass
        finally:
            self._lease = None


__all__ = ["SessionStreamOwnership"]
