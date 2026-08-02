"""Generic in-memory and crash-durable lease coordinators."""

from __future__ import annotations

import asyncio
import fcntl
import json
import math
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from mote.contracts.clock import UNIX_UTC_CLOCK, AbsoluteInstant
from mote.contracts.ports.runtime.lease import LeaseCoordinator, LeaseEpoch
from mote.contracts.runtime.errors import LeaseCoordinatorUnavailableError, LeaseFencedError, LeaseUnavailableError
from mote.contracts.runtime.lease import RuntimeLease, RuntimeLeasePolicy
from mote.runtime.persistence import disk_io

_FILE_LEASE_SCHEMA = "mote.file-lease-coordinator/v1"
_FILE_LEASE_SCHEMA_VERSION = 1


class InMemoryLeaseCoordinator:
    """Process-local coordinator with production-equivalent token semantics."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._leases: dict[str, RuntimeLease] = {}
        self._lock = threading.RLock()

    def acquire(self, subject: str, owner_id: str, ttl_seconds: float) -> RuntimeLease:
        _validate_request(subject, owner_id, ttl_seconds)
        with self._lock:
            lease = _acquire(self._leases, subject, owner_id, ttl_seconds, self._clock())
            self._leases[subject] = lease
            return lease

    def renew(self, lease: LeaseEpoch, ttl_seconds: float) -> RuntimeLease:
        _validate_request(lease.subject, lease.owner_id, ttl_seconds)
        with self._lock:
            _assert_current(self._leases, lease.subject, lease.fencing_token, self._clock(), lease.owner_id)
            renewed = RuntimeLease(lease.subject, lease.owner_id, lease.fencing_token, self._clock() + ttl_seconds)
            self._leases[lease.subject] = renewed
            return renewed

    def release(self, lease: LeaseEpoch) -> None:
        with self._lock:
            _assert_current(self._leases, lease.subject, lease.fencing_token, self._clock(), lease.owner_id)
            self._leases[lease.subject] = RuntimeLease(lease.subject, "", lease.fencing_token, self._clock())

    def assert_current(self, subject: str, fencing_token: int) -> None:
        with self._lock:
            _assert_current(self._leases, subject, fencing_token, self._clock())

    @contextmanager
    def guard(self, subject: str, fencing_token: int) -> Iterator[None]:
        with self._lock:
            _assert_current(self._leases, subject, fencing_token, self._clock())
            yield

    @contextmanager
    def guard_many(self, bindings: tuple[tuple[str, int], ...]) -> Iterator[None]:
        with self._lock:
            for subject, fencing_token in bindings:
                _assert_current(self._leases, subject, fencing_token, self._clock())
            yield

    def get(self, subject: str) -> RuntimeLease | None:
        with self._lock:
            return self._leases.get(subject)


class FileLeaseCoordinator:
    """File-backed coordinator with cross-process locks and monotonic tokens."""

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self._path = path
        self._lock_path = path.with_name(f"{path.name}.lock")
        self._clock = clock

    def acquire(self, subject: str, owner_id: str, ttl_seconds: float) -> RuntimeLease:
        _validate_request(subject, owner_id, ttl_seconds)
        with self._locked():
            leases = self._read()
            lease = _acquire(leases, subject, owner_id, ttl_seconds, self._clock())
            leases[subject] = lease
            self._write(leases)
            return lease

    def renew(self, lease: LeaseEpoch, ttl_seconds: float) -> RuntimeLease:
        _validate_request(lease.subject, lease.owner_id, ttl_seconds)
        with self._locked():
            leases = self._read()
            _assert_current(leases, lease.subject, lease.fencing_token, self._clock(), lease.owner_id)
            renewed = RuntimeLease(lease.subject, lease.owner_id, lease.fencing_token, self._clock() + ttl_seconds)
            leases[lease.subject] = renewed
            self._write(leases)
            return renewed

    def release(self, lease: LeaseEpoch) -> None:
        with self._locked():
            leases = self._read()
            _assert_current(leases, lease.subject, lease.fencing_token, self._clock(), lease.owner_id)
            leases[lease.subject] = RuntimeLease(lease.subject, "", lease.fencing_token, self._clock())
            self._write(leases)

    def assert_current(self, subject: str, fencing_token: int) -> None:
        with self._locked():
            _assert_current(self._read(), subject, fencing_token, self._clock())

    @contextmanager
    def guard(self, subject: str, fencing_token: int) -> Iterator[None]:
        with self._locked():
            _assert_current(self._read(), subject, fencing_token, self._clock())
            yield

    @contextmanager
    def guard_many(self, bindings: tuple[tuple[str, int], ...]) -> Iterator[None]:
        with self._locked():
            leases = self._read()
            now = self._clock()
            for subject, fencing_token in bindings:
                _assert_current(leases, subject, fencing_token, now)
            yield

    def get(self, subject: str) -> RuntimeLease | None:
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

    def _read(self) -> dict[str, RuntimeLease]:
        try:
            raw = self._load_json()
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise self._unavailable("invalid lease state", exc) from exc
        try:
            return self._decode_v1(raw)
        except (KeyError, TypeError, ValueError) as exc:
            raise self._unavailable("invalid lease record", exc) from exc

    def _load_json(self) -> object:
        return json.loads(self._path.read_text(encoding="utf-8"))

    @staticmethod
    def _decode_v1(raw: object) -> dict[str, RuntimeLease]:
        if type(raw) is not dict or set(raw) != {"schema", "schema_version", "leases"}:
            raise ValueError("lease envelope fields are not canonical")
        assert isinstance(raw, dict)
        if raw["schema"] != _FILE_LEASE_SCHEMA:
            raise ValueError("unknown lease state schema")
        if type(raw["schema_version"]) is not int or raw["schema_version"] != _FILE_LEASE_SCHEMA_VERSION:
            raise ValueError("unsupported lease state version")
        records = raw["leases"]
        if type(records) is not dict:
            raise ValueError("lease records must be an object")
        leases: dict[str, RuntimeLease] = {}
        for subject, record in records.items():
            if type(subject) is not str or not subject:
                raise ValueError("lease index subject is invalid")
            lease = FileLeaseCoordinator._decode_record(record)
            if lease.subject != subject:
                raise ValueError("lease subject does not match its index identity")
            leases[subject] = lease
        return leases

    @staticmethod
    def _decode_record(record: object) -> RuntimeLease:
        if type(record) is not dict or set(record) != {
            "subject",
            "owner_id",
            "fencing_token",
            "expires_at",
        }:
            raise ValueError("lease record fields are not canonical")
        assert isinstance(record, dict)
        subject = record["subject"]
        owner_id = record["owner_id"]
        token = record["fencing_token"]
        if type(subject) is not str or not subject:
            raise ValueError("lease subject is invalid")
        if type(owner_id) is not str:
            raise ValueError("lease owner_id is invalid")
        if type(token) is not int or token < 1:
            raise ValueError("lease fencing_token is invalid")
        expires_at = AbsoluteInstant.from_dict(record["expires_at"])
        expires_at.require_clock(UNIX_UTC_CLOCK)
        return RuntimeLease(
            subject,
            owner_id,
            token,
            expires_at.epoch_nanoseconds / 1_000_000_000,
            expires_at,
        )

    def _write(self, leases: dict[str, RuntimeLease]) -> None:
        payload = {
            "schema": _FILE_LEASE_SCHEMA,
            "schema_version": _FILE_LEASE_SCHEMA_VERSION,
            "leases": {
                subject: {
                    "subject": subject,
                    "owner_id": lease.owner_id,
                    "fencing_token": lease.fencing_token,
                    "expires_at": lease.durable_expiry().to_dict(),
                }
                for subject, lease in sorted(leases.items())
            },
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
        policy: RuntimeLeasePolicy = RuntimeLeasePolicy(),
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

    async def adopt(self, lease: LeaseEpoch) -> "LeaseHandle":
        """Manage an epoch already acquired by the same coordinator owner."""
        if self.lease is not None or self._heartbeat is not None:
            raise RuntimeError("lease handle is already active")
        if lease.subject != self.subject or lease.owner_id != self.owner_id:
            raise ValueError("adopted lease identity does not match the handle")
        self.coordinator.assert_current(lease.subject, lease.fencing_token)
        self.lease = lease
        self._stop.clear()
        self._heartbeat = asyncio.create_task(self._renew_loop())
        return self

    async def wait_for_loss(self) -> None:
        """Wait until heartbeat stops and fail closed if renewal was lost."""
        task = self._heartbeat
        if task is None:
            raise RuntimeError("lease handle is not active")
        await asyncio.shield(task)
        self._raise_renew_error()
        raise RuntimeError("lease heartbeat stopped without ownership loss")

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
    if type(subject) is not str or not subject or type(owner_id) is not str or not owner_id:
        raise ValueError("lease subject and owner_id must be non-empty")
    if type(ttl_seconds) not in {int, float} or not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
        raise ValueError("lease ttl_seconds must be positive")


def _acquire(
    leases: dict[str, RuntimeLease],
    subject: str,
    owner_id: str,
    ttl_seconds: float,
    now: float,
) -> RuntimeLease:
    current = leases.get(subject)
    if current is not None and current.owner_id == owner_id and current.expires_at > now:
        return RuntimeLease(subject, owner_id, current.fencing_token, now + ttl_seconds)
    if current is not None and current.owner_id and current.expires_at > now:
        raise LeaseUnavailableError(
            "subject is owned by another live owner",
            subject=subject,
            owner_id=current.owner_id,
            expires_at=current.expires_at,
        )
    token = 1 if current is None else current.fencing_token + 1
    return RuntimeLease(subject, owner_id, token, now + ttl_seconds)


def _assert_current(
    leases: dict[str, RuntimeLease],
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
