"""Generic in-memory and crash-durable lease coordinators."""
from __future__ import annotations

import asyncio
import fcntl
import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from mote.contracts.errors.runtimes import LeaseCoordinatorUnavailableError, LeaseFencedError, LeaseUnavailableError
from mote.contracts.leases import Lease, LeasePolicy
from mote.contracts.ports.lease import LeaseCoordinator, LeaseEpoch
from mote.runtime.disk import disk_io


class InMemoryLeaseCoordinator:
    """Process-local coordinator with production-equivalent token semantics."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._leases: dict[str, Lease] = {}
        self._lock = threading.RLock()

    def acquire(self, subject: str, owner_id: str, ttl_seconds: float) -> Lease:
        _validate_request(subject, owner_id, ttl_seconds)
        with self._lock:
            lease = _acquire(self._leases, subject, owner_id, ttl_seconds, self._clock())
            self._leases[subject] = lease
            return lease

    def renew(self, lease: LeaseEpoch, ttl_seconds: float) -> Lease:
        _validate_request(lease.subject, lease.owner_id, ttl_seconds)
        with self._lock:
            _assert_current(self._leases, lease.subject, lease.fencing_token, self._clock(), lease.owner_id)
            renewed = Lease(lease.subject, lease.owner_id, lease.fencing_token, self._clock() + ttl_seconds)
            self._leases[lease.subject] = renewed
            return renewed

    def release(self, lease: LeaseEpoch) -> None:
        with self._lock:
            _assert_current(self._leases, lease.subject, lease.fencing_token, self._clock(), lease.owner_id)
            self._leases[lease.subject] = Lease(lease.subject, "", lease.fencing_token, self._clock())

    def assert_current(self, subject: str, fencing_token: int) -> None:
        with self._lock:
            _assert_current(self._leases, subject, fencing_token, self._clock())

    @contextmanager
    def guard(self, subject: str, fencing_token: int) -> Iterator[None]:
        with self._lock:
            _assert_current(self._leases, subject, fencing_token, self._clock())
            yield

    def get(self, subject: str) -> Lease | None:
        with self._lock:
            return self._leases.get(subject)


class FileLeaseCoordinator:
    """File-backed coordinator with cross-process locks and monotonic tokens."""

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self._path = path
        self._lock_path = path.with_name(f"{path.name}.lock")
        self._clock = clock

    def acquire(self, subject: str, owner_id: str, ttl_seconds: float) -> Lease:
        _validate_request(subject, owner_id, ttl_seconds)
        with self._locked():
            leases = self._read()
            lease = _acquire(leases, subject, owner_id, ttl_seconds, self._clock())
            leases[subject] = lease
            self._write(leases)
            return lease

    def renew(self, lease: LeaseEpoch, ttl_seconds: float) -> Lease:
        _validate_request(lease.subject, lease.owner_id, ttl_seconds)
        with self._locked():
            leases = self._read()
            _assert_current(leases, lease.subject, lease.fencing_token, self._clock(), lease.owner_id)
            renewed = Lease(lease.subject, lease.owner_id, lease.fencing_token, self._clock() + ttl_seconds)
            leases[lease.subject] = renewed
            self._write(leases)
            return renewed

    def release(self, lease: LeaseEpoch) -> None:
        with self._locked():
            leases = self._read()
            _assert_current(leases, lease.subject, lease.fencing_token, self._clock(), lease.owner_id)
            leases[lease.subject] = Lease(lease.subject, "", lease.fencing_token, self._clock())
            self._write(leases)

    def assert_current(self, subject: str, fencing_token: int) -> None:
        with self._locked():
            _assert_current(self._read(), subject, fencing_token, self._clock())

    @contextmanager
    def guard(self, subject: str, fencing_token: int) -> Iterator[None]:
        with self._locked():
            _assert_current(self._read(), subject, fencing_token, self._clock())
            yield

    def get(self, subject: str) -> Lease | None:
        with self._locked():
            return self._read().get(subject)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        try:
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_file = self._lock_path.open("a+b")
        except OSError as exc:
            raise self._unavailable("cannot open lease lock", exc) from exc
        with lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except LeaseCoordinatorUnavailableError:
                raise
            except OSError as exc:
                raise self._unavailable("lease lock operation failed", exc) from exc

    def _read(self) -> dict[str, Lease]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise self._unavailable("invalid lease state", exc) from exc
        try:
            return {
                subject: Lease(
                    subject=subject,
                    owner_id=str(item["owner_id"]),
                    fencing_token=int(item["fencing_token"]),
                    expires_at=float(item["expires_at"]),
                )
                for subject, item in raw.items()
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise self._unavailable("invalid lease record", exc) from exc

    def _write(self, leases: dict[str, Lease]) -> None:
        payload = {
            subject: {
                "owner_id": lease.owner_id,
                "fencing_token": lease.fencing_token,
                "expires_at": lease.expires_at,
            }
            for subject, lease in leases.items()
        }
        try:
            disk_io.atomic_write(
                self._path,
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
                fsync=True,
            )
        except OSError as exc:
            raise self._unavailable("cannot persist lease state", exc) from exc

    def _unavailable(self, message: str, cause: Exception) -> LeaseCoordinatorUnavailableError:
        return LeaseCoordinatorUnavailableError(message, path=str(self._path), cause=cause)


class LeaseHandle:
    """One live lease epoch with heartbeat and commit fencing."""

    def __init__(
        self,
        coordinator: LeaseCoordinator,
        *,
        subject: str,
        owner_id: str,
        policy: LeasePolicy = LeasePolicy(),
    ) -> None:
        self.coordinator = coordinator
        self.subject = subject
        self.owner_id = owner_id
        self.policy = policy
        self.lease: LeaseEpoch | None = None
        self._heartbeat: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._renew_error: Exception | None = None

    @property
    def fencing_token(self) -> int:
        if self.lease is None:
            raise RuntimeError("lease has not been started")
        return self.lease.fencing_token

    async def start(self) -> "LeaseHandle":
        if self.lease is not None:
            return self
        self.lease = self.coordinator.acquire(self.subject, self.owner_id, self.policy.ttl_seconds)
        self._stop.clear()
        self._heartbeat = asyncio.create_task(self._renew_loop())
        return self

    async def close(self) -> None:
        task, self._heartbeat = self._heartbeat, None
        if task is not None:
            self._stop.set()
            await task
        lease, self.lease = self.lease, None
        if lease is not None:
            try:
                self.coordinator.release(lease)
            except (LeaseFencedError, LeaseCoordinatorUnavailableError):
                pass

    def assert_current(self) -> None:
        self._raise_renew_error()
        self.coordinator.assert_current(self.subject, self.fencing_token)

    @contextmanager
    def guard(self) -> Iterator[None]:
        self._raise_renew_error()
        with self.coordinator.guard(self.subject, self.fencing_token):
            yield

    async def _renew_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.policy.renew_interval_seconds)
                return
            except TimeoutError:
                pass
            assert self.lease is not None
            try:
                self.lease = self.coordinator.renew(self.lease, self.policy.ttl_seconds)
            except Exception as exc:
                self._renew_error = exc
                return

    def _raise_renew_error(self) -> None:
        if self._renew_error is not None:
            raise LeaseCoordinatorUnavailableError(
                "lease heartbeat failed; ownership can no longer be proven",
                subject=self.subject,
                owner_id=self.owner_id,
                cause=self._renew_error,
            ) from self._renew_error


def _validate_request(subject: str, owner_id: str, ttl_seconds: float) -> None:
    if not subject or not owner_id:
        raise ValueError("lease subject and owner_id must be non-empty")
    if ttl_seconds <= 0:
        raise ValueError("lease ttl_seconds must be positive")


def _acquire(
    leases: dict[str, Lease],
    subject: str,
    owner_id: str,
    ttl_seconds: float,
    now: float,
) -> Lease:
    current = leases.get(subject)
    if current is not None and current.owner_id == owner_id and current.expires_at > now:
        return Lease(subject, owner_id, current.fencing_token, now + ttl_seconds)
    if current is not None and current.owner_id and current.expires_at > now:
        raise LeaseUnavailableError(
            "subject is owned by another live owner",
            subject=subject,
            owner_id=current.owner_id,
            expires_at=current.expires_at,
        )
    token = 1 if current is None else current.fencing_token + 1
    return Lease(subject, owner_id, token, now + ttl_seconds)


def _assert_current(
    leases: dict[str, Lease],
    subject: str,
    fencing_token: int,
    now: float,
    owner_id: str | None = None,
) -> None:
    current = leases.get(subject)
    if (
        current is None
        or not current.owner_id
        or current.fencing_token != fencing_token
        or current.expires_at <= now
        or (owner_id is not None and current.owner_id != owner_id)
    ):
        raise LeaseFencedError(
            "lease is absent, expired, or superseded",
            subject=subject,
            fencing_token=fencing_token,
            current_fencing_token=(current.fencing_token if current else None),
        )


__all__ = ["FileLeaseCoordinator", "InMemoryLeaseCoordinator", "LeaseHandle"]
