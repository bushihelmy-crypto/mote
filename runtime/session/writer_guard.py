"""Canonical Session/run writer guard backed by one Runtime lease store."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from mote.contracts.ports.events.journal import StreamWriterFence, StreamWriterFenced
from mote.contracts.ports.runtime.lease import LeaseCoordinator, LeaseEpoch
from mote.contracts.runtime.errors import LeaseFencedError
from mote.contracts.runtime.lease import RuntimeLeasePolicy
from mote.runtime.control.leases import LeaseHandle
from mote.runtime.persistence.async_io import run_disk_io


class SessionRunWriterGuard:
    """Own the Session stream epoch and validate run epochs under the same lock."""

    def __init__(
        self,
        coordinator: LeaseCoordinator,
        *,
        session_id: str,
        owner_id: str,
        incarnation_id: str,
        ttl_seconds: float = 30.0,
    ) -> None:
        if not session_id or not owner_id or not incarnation_id or ttl_seconds <= 0:
            raise ValueError("session run writer guard identity is invalid")
        self._coordinator = coordinator
        self._stream_subject = f"session-stream:{session_id}"
        self._owner_id = owner_id
        self._incarnation_id = incarnation_id
        self._ttl_seconds = ttl_seconds
        self._stream_lease: LeaseEpoch | None = None
        self._stream_handle: LeaseHandle | None = None

    @property
    def lifecycle_generation(self) -> int:
        if self._stream_lease is None:
            raise RuntimeError("Session stream writer has not acquired ownership")
        return self._stream_lease.fencing_token

    @property
    def owner_id(self) -> str:
        return self._owner_id

    @property
    def incarnation_id(self) -> str:
        return self._incarnation_id

    async def start(self) -> None:
        if self._stream_handle is not None:
            return
        interval = min(self._ttl_seconds / 3, self._ttl_seconds - 0.001)
        handle = LeaseHandle(
            self._coordinator,
            subject=self._stream_subject,
            owner_id=self._owner_id,
            policy=RuntimeLeasePolicy(
                ttl_seconds=self._ttl_seconds,
                renew_interval_seconds=interval,
            ),
        )
        if self._stream_lease is None:
            self._stream_lease = await run_disk_io(
                self._coordinator.acquire,
                self._stream_subject,
                self._owner_id,
                self._ttl_seconds,
            )
        self._stream_handle = handle.adopt_nowait(self._stream_lease)

    async def aclose(self) -> None:
        handle, self._stream_handle = self._stream_handle, None
        if handle is not None:
            await handle.close()
            self._stream_lease = None
            return
        self.release()

    def release(self) -> None:
        lease, self._stream_lease = self._stream_lease, None
        if lease is None:
            return
        try:
            self._coordinator.release(lease)
        except LeaseFencedError:
            pass

    def acquire_run(self, run_id: str) -> StreamWriterFence:
        lease = self._coordinator.acquire(self._run_subject(run_id), self._owner_id, self._ttl_seconds)
        return StreamWriterFence(run_id, self._owner_id, self._incarnation_id, lease.fencing_token)

    def writer_for(self, run_id: str, fencing_token: int) -> StreamWriterFence:
        if type(fencing_token) is not int or fencing_token < 1:
            raise ValueError("run fencing token must be positive")
        return StreamWriterFence(run_id, self._owner_id, self._incarnation_id, fencing_token)

    def release_run(self, writer: StreamWriterFence) -> None:
        if writer.owner_id != self._owner_id or writer.incarnation_id != self._incarnation_id:
            raise StreamWriterFenced("writer owner or incarnation is stale")
        try:
            self._coordinator.release(_WriterEpoch(self._run_subject(writer.run_id), writer))
        except LeaseFencedError as error:
            raise StreamWriterFenced(str(error)) from error

    @contextmanager
    def guard(self) -> Iterator[None]:
        if self._stream_lease is None:
            self._stream_lease = self._coordinator.acquire(
                self._stream_subject,
                self._owner_id,
                self._ttl_seconds,
            )
        elif self._stream_handle is None:
            self._stream_lease = self._coordinator.renew(self._stream_lease, self._ttl_seconds)
        guard = (
            self._stream_handle.guard()
            if self._stream_handle is not None
            else self._coordinator.guard(self._stream_subject, self._stream_lease.fencing_token)
        )
        with guard:
            yield

    @contextmanager
    def guard_append(self, writer: StreamWriterFence) -> Iterator[None]:
        """Atomically fence the Session stream and exact run writer."""
        if writer.owner_id != self._owner_id or writer.incarnation_id != self._incarnation_id:
            raise StreamWriterFenced("writer owner or incarnation is stale")
        try:
            if self._stream_lease is None:
                self._stream_lease = self._coordinator.acquire(
                    self._stream_subject,
                    self._owner_id,
                    self._ttl_seconds,
                )
            elif self._stream_handle is None:
                self._stream_lease = self._coordinator.renew(self._stream_lease, self._ttl_seconds)
            with self._coordinator.guard_many(
                (
                    (self._stream_subject, self._stream_lease.fencing_token),
                    (self._run_subject(writer.run_id), writer.fencing_token),
                )
            ):
                yield
        except LeaseFencedError as error:
            raise StreamWriterFenced(str(error)) from error

    @staticmethod
    def _run_subject(run_id: str) -> str:
        if type(run_id) is not str or not run_id:
            raise ValueError("run_id must be a non-empty string")
        return f"session-run:{run_id}"


__all__ = ["SessionRunWriterGuard"]


@dataclass(frozen=True, slots=True)
class _WriterEpoch:
    subject: str
    writer: StreamWriterFence
    expires_at: float = 1.0

    @property
    def owner_id(self) -> str:
        return self.writer.owner_id

    @property
    def fencing_token(self) -> int:
        return self.writer.fencing_token
