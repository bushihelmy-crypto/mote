"""OS-held, monotonic fenced lease for the durable Cron scheduler."""

from __future__ import annotations

import fcntl
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional
from uuid import uuid4

from mote.contracts.clock import AbsoluteInstant
from mote.contracts.ports.clock import ClockSource
from mote.runtime.telemetry.logging import log_class

LOCK_FILENAME = "scheduled_tasks.lock"
_LOCK_SCHEMA = "mote.cron-scheduler-lease/v1"
_LOCK_FIELDS = {
    "schema",
    "epoch",
    "token",
    "owner_instance_id",
    "status",
    "refreshed_at",
}


class SchedulerLockError(RuntimeError):
    pass


class SchedulerLockCorruptionError(SchedulerLockError):
    pass


class SchedulerFenceLost(SchedulerLockError):
    pass


@dataclass(frozen=True, slots=True)
class SchedulerFence:
    epoch: int
    token: str
    owner_instance_id: str

    def __post_init__(self) -> None:
        if type(self.epoch) is not int or self.epoch < 1:
            raise ValueError("scheduler fence epoch is invalid")
        for name in ("token", "owner_instance_id"):
            value = getattr(self, name)
            if type(value) is not str or not value:
                raise ValueError(f"scheduler fence {name} is invalid")


@dataclass(frozen=True, slots=True)
class _LeaseRecord:
    epoch: int
    token: str
    owner_instance_id: str
    status: Literal["active", "released"]
    refreshed_at: AbsoluteInstant


def _decode_record(raw: str) -> _LeaseRecord:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise SchedulerLockCorruptionError("scheduler lease JSON is corrupt") from error
    if type(value) is not dict or set(value) != _LOCK_FIELDS:
        raise SchedulerLockCorruptionError("scheduler lease fields are not canonical")
    if value["schema"] != _LOCK_SCHEMA:
        raise SchedulerLockCorruptionError("unsupported scheduler lease schema")
    if type(value["epoch"]) is not int or value["epoch"] < 1:
        raise SchedulerLockCorruptionError("scheduler lease epoch is invalid")
    for name in ("token", "owner_instance_id", "status"):
        if type(value[name]) is not str:
            raise SchedulerLockCorruptionError(f"scheduler lease {name} primitive is invalid")
    if value["status"] not in {"active", "released"}:
        raise SchedulerLockCorruptionError("scheduler lease status is invalid")
    try:
        refreshed_at = AbsoluteInstant.from_dict(value["refreshed_at"])
    except ValueError as error:
        raise SchedulerLockCorruptionError("scheduler lease instant is invalid") from error
    return _LeaseRecord(
        epoch=value["epoch"],
        token=value["token"],
        owner_instance_id=value["owner_instance_id"],
        status=value["status"],
        refreshed_at=refreshed_at,
    )


def _encode_record(record: _LeaseRecord) -> bytes:
    return (
        json.dumps(
            {
                "schema": _LOCK_SCHEMA,
                "epoch": record.epoch,
                "token": record.token,
                "owner_instance_id": record.owner_instance_id,
                "status": record.status,
                "refreshed_at": record.refreshed_at.to_dict(),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _read_path(path: Path) -> _LeaseRecord:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise SchedulerFenceLost("scheduler lease cannot be read") from error
    return _decode_record(raw)


def verify_scheduler_fence(base_dir: Path, fence: SchedulerFence) -> None:
    record = _read_path(base_dir / LOCK_FILENAME)
    if (
        record.status != "active"
        or record.epoch != fence.epoch
        or record.token != fence.token
        or record.owner_instance_id != fence.owner_instance_id
    ):
        raise SchedulerFenceLost("scheduler mutation fence is stale")


@log_class(level="DEBUG", exclude={"path", "is_held"})
class SchedulerLock:
    """Holds an advisory OS lock for the full scheduler ownership lifetime."""

    def __init__(
        self,
        session_id: str,
        base_dir: str,
        *,
        clock_source: ClockSource,
    ) -> None:
        if not session_id:
            raise ValueError("scheduler owner identity must not be empty")
        self.session_id = session_id
        self._owner_instance_id = uuid4().hex
        self._dir = Path(base_dir)
        self._path = self._dir / LOCK_FILENAME
        self._clock = clock_source
        self._descriptor: int | None = None
        self._fence: SchedulerFence | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def is_held(self) -> bool:
        return self._descriptor is not None

    @property
    def fence(self) -> SchedulerFence | None:
        return self._fence

    def _read_descriptor(self, descriptor: int) -> _LeaseRecord | None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        raw = os.read(descriptor, 64 * 1024)
        if not raw:
            return None
        try:
            return _decode_record(raw.decode("utf-8"))
        except UnicodeDecodeError as error:
            raise SchedulerLockCorruptionError("scheduler lease is not UTF-8") from error

    def _write_descriptor(self, descriptor: int, record: _LeaseRecord) -> None:
        payload = _encode_record(record)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.ftruncate(descriptor, 0)
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
        os.fsync(descriptor)

    def acquire(self) -> SchedulerFence | None:
        if self._fence is not None:
            return self._fence
        self._dir.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return None
        try:
            prior = self._read_descriptor(descriptor)
            epoch = 1 if prior is None else prior.epoch + 1
            fence = SchedulerFence(epoch, uuid4().hex, self._owner_instance_id)
            self._write_descriptor(
                descriptor,
                _LeaseRecord(
                    epoch=fence.epoch,
                    token=fence.token,
                    owner_instance_id=fence.owner_instance_id,
                    status="active",
                    refreshed_at=self._clock.now(),
                ),
            )
        except BaseException:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            raise
        self._descriptor = descriptor
        self._fence = fence
        return fence

    def refresh(self) -> None:
        descriptor = self._descriptor
        fence = self._fence
        if descriptor is None or fence is None:
            raise SchedulerFenceLost("scheduler lease is not held")
        current = self._read_descriptor(descriptor)
        if current is None or (
            current.status != "active"
            or current.epoch != fence.epoch
            or current.token != fence.token
            or current.owner_instance_id != fence.owner_instance_id
        ):
            raise SchedulerFenceLost("scheduler refresh fence is stale")
        self._write_descriptor(
            descriptor,
            _LeaseRecord(
                fence.epoch,
                fence.token,
                fence.owner_instance_id,
                "active",
                self._clock.now(),
            ),
        )

    def release(self) -> None:
        descriptor = self._descriptor
        fence = self._fence
        if descriptor is None or fence is None:
            raise SchedulerFenceLost("scheduler lease is not held")
        current = self._read_descriptor(descriptor)
        if current is None or (
            current.status != "active"
            or current.epoch != fence.epoch
            or current.token != fence.token
            or current.owner_instance_id != fence.owner_instance_id
        ):
            raise SchedulerFenceLost("scheduler release fence is stale")
        self._write_descriptor(
            descriptor,
            _LeaseRecord(
                fence.epoch,
                fence.token,
                fence.owner_instance_id,
                "released",
                self._clock.now(),
            ),
        )
        self._fence = None
        self._descriptor = None
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


__all__ = [
    "LOCK_FILENAME",
    "SchedulerFence",
    "SchedulerFenceLost",
    "SchedulerLock",
    "SchedulerLockCorruptionError",
    "SchedulerLockError",
    "verify_scheduler_fence",
]
