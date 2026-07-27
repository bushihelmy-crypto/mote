"""Crash-durable, cross-process run leases with monotonic fencing tokens."""
from __future__ import annotations

import asyncio
import fcntl
import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from mote.contracts.leases import RunLease, RunLeasePolicy
from mote.contracts.ports import LeaseEpoch, RunLeaseCoordinator
from mote.runtime.disk import disk_io
from mote.runtime.errors import OutputCommitFencedError, RunLeaseCoordinatorUnavailableError, RunLeaseUnavailableError
from mote.runtime.events import RunLeaseEvent, observe_event


class RunLeaseStore:
    """A file-backed lease coordinator scoped to one session.

    State changes and commit guards use the same advisory lock. A fencing token
    is retained after release and incremented on every new ownership epoch.
    """

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self._path = path
        self._lock_path = path.with_name(f"{path.name}.lock")
        self._clock = clock

    def acquire(self, run_id: str, owner_id: str, ttl_seconds: float) -> RunLease:
        self._validate_request(run_id, owner_id, ttl_seconds)
        with self._locked():
            leases = self._read()
            current = leases.get(run_id)
            now = self._clock()
            if current is not None and current.owner_id == owner_id and current.expires_at > now:
                lease = RunLease(run_id, owner_id, current.fencing_token, now + ttl_seconds)
            elif current is not None and current.owner_id and current.expires_at > now:
                raise RunLeaseUnavailableError(
                    "run is owned by another live worker",
                    run_id=run_id,
                    owner_id=current.owner_id,
                    expires_at=current.expires_at,
                )
            else:
                token = 1 if current is None else current.fencing_token + 1
                lease = RunLease(run_id, owner_id, token, now + ttl_seconds)
            leases[run_id] = lease
            self._write(leases)
            return lease

    def renew(self, lease: LeaseEpoch, ttl_seconds: float) -> RunLease:
        self._validate_request(lease.run_id, lease.owner_id, ttl_seconds)
        with self._locked():
            leases = self._read()
            self._assert(leases, lease.run_id, lease.fencing_token, lease.owner_id)
            renewed = RunLease(
                lease.run_id,
                lease.owner_id,
                lease.fencing_token,
                self._clock() + ttl_seconds,
            )
            leases[lease.run_id] = renewed
            self._write(leases)
            return renewed

    def release(self, lease: LeaseEpoch) -> None:
        with self._locked():
            leases = self._read()
            self._assert(leases, lease.run_id, lease.fencing_token, lease.owner_id)
            leases[lease.run_id] = RunLease(lease.run_id, "", lease.fencing_token, self._clock())
            self._write(leases)

    def assert_current(self, run_id: str, fencing_token: int) -> None:
        with self._locked():
            self._assert(self._read(), run_id, fencing_token)

    @contextmanager
    def guard(self, run_id: str, fencing_token: int) -> Iterator[None]:
        """Hold takeover serialization through the caller's commit write."""
        with self._locked():
            self._assert(self._read(), run_id, fencing_token)
            yield

    def get(self, run_id: str) -> RunLease | None:
        with self._locked():
            return self._read().get(run_id)

    def _assert(
        self,
        leases: dict[str, RunLease],
        run_id: str,
        fencing_token: int,
        owner_id: str | None = None,
    ) -> None:
        current = leases.get(run_id)
        if (
            current is None
            or not current.owner_id
            or current.fencing_token != fencing_token
            or current.expires_at <= self._clock()
            or (owner_id is not None and current.owner_id != owner_id)
        ):
            raise OutputCommitFencedError(
                "run lease is absent, expired, or superseded",
                run_id=run_id,
                fencing_token=fencing_token,
                current_fencing_token=(current.fencing_token if current else None),
            )

    @contextmanager
    def _locked(self) -> Iterator[None]:
        try:
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_file = self._lock_path.open("a+b")
        except OSError as exc:
            raise self._unavailable("cannot open run-lease lock", exc) from exc
        with lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except RunLeaseCoordinatorUnavailableError:
                raise
            except OSError as exc:
                raise self._unavailable("run-lease lock operation failed", exc) from exc

    def _unavailable(self, message: str, cause: Exception) -> RunLeaseCoordinatorUnavailableError:
        return RunLeaseCoordinatorUnavailableError(message, path=str(self._path), cause=cause)

    def _read(self) -> dict[str, RunLease]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise self._unavailable("invalid run-lease state", exc) from exc
        try:
            return {
                run_id: RunLease(
                    run_id=run_id,
                    owner_id=str(item["owner_id"]),
                    fencing_token=int(item["fencing_token"]),
                    expires_at=float(item["expires_at"]),
                )
                for run_id, item in raw.items()
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise self._unavailable("invalid run-lease record", exc) from exc

    def _write(self, leases: dict[str, RunLease]) -> None:
        payload = {
            run_id: {
                "owner_id": lease.owner_id,
                "fencing_token": lease.fencing_token,
                "expires_at": lease.expires_at,
            }
            for run_id, lease in leases.items()
        }
        try:
            disk_io.atomic_write(
                self._path,
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(),
                fsync=True,
            )
        except OSError as exc:
            raise self._unavailable("cannot persist run-lease state", exc) from exc

    @staticmethod
    def _validate_request(run_id: str, owner_id: str, ttl_seconds: float) -> None:
        if not run_id or not owner_id:
            raise ValueError("run_id and owner_id must be non-empty")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")


class RunLeaseHandle:
    """One live ownership epoch with an event-loop heartbeat."""

    def __init__(
        self,
        store: RunLeaseCoordinator,
        *,
        run_id: str,
        owner_id: str,
        policy: RunLeasePolicy = RunLeasePolicy(),
    ) -> None:
        self.store = store
        self.run_id = run_id
        self.owner_id = owner_id
        self.policy = policy
        self.lease: LeaseEpoch | None = None
        self._heartbeat: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._lost = False
        self._renew_error: Exception | None = None

    @property
    def fencing_token(self) -> int:
        if self.lease is None:
            raise RuntimeError("run lease has not been started")
        return self.lease.fencing_token

    async def start(self) -> "RunLeaseHandle":
        if self.lease is not None:
            return self
        self.lease = self.store.acquire(self.run_id, self.owner_id, self.ttl_seconds)
        self._stop.clear()
        self._heartbeat = asyncio.create_task(self._renew_loop())
        await self._observe("acquired")
        return self

    async def close(self) -> None:
        task, self._heartbeat = self._heartbeat, None
        if task is not None:
            self._stop.set()
            await task
        lease, self.lease = self.lease, None
        if lease is not None:
            try:
                self.store.release(lease)
            except OutputCommitFencedError:
                if not self._lost:
                    self._lost = True
                    await self._observe("lost", reason="release_fenced", lease=lease)
            except Exception:
                self._lost = True
                await self._observe("lost", reason="release_coordinator_unavailable", lease=lease)
            else:
                await self._observe("released", lease=lease)

    def assert_current(self, run_id: str, fencing_token: int) -> None:
        self._assert_identity(run_id, fencing_token)
        self._raise_renew_error()
        self.store.assert_current(run_id, fencing_token)

    @contextmanager
    def guard(self, run_id: str, fencing_token: int) -> Iterator[None]:
        self._assert_identity(run_id, fencing_token)
        self._raise_renew_error()
        with self.store.guard(run_id, fencing_token):
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
                self.lease = self.store.renew(self.lease, self.ttl_seconds)
            except OutputCommitFencedError:
                self._lost = True
                await self._observe("lost", reason="renew_fenced")
                return
            except Exception as exc:
                self._lost = True
                self._renew_error = exc
                await self._observe("lost", reason="coordinator_unavailable")
                return
            await self._observe("renewed")

    def _raise_renew_error(self) -> None:
        if self._renew_error is not None:
            raise RunLeaseCoordinatorUnavailableError(
                "run lease heartbeat failed; ownership can no longer be proven",
                run_id=self.run_id,
                owner_id=self.owner_id,
                cause=self._renew_error,
            ) from self._renew_error

    async def _observe(self, phase: str, *, reason: str = "", lease: LeaseEpoch | None = None) -> None:
        current = lease or self.lease
        await observe_event(
            RunLeaseEvent(
                phase=phase,
                run_id=self.run_id,
                owner_id=self.owner_id,
                fencing_token=(current.fencing_token if current else 0),
                expires_at=(current.expires_at if current else 0.0),
                reason=reason,
            )
        )

    def _assert_identity(self, run_id: str, fencing_token: int) -> None:
        if run_id != self.run_id or fencing_token != self.fencing_token:
            raise OutputCommitFencedError(
                "commit does not belong to this lease handle",
                run_id=run_id,
                fencing_token=fencing_token,
            )

    @property
    def ttl_seconds(self) -> float:
        return self.policy.ttl_seconds


__all__ = ["RunLeaseHandle", "RunLeaseStore"]
