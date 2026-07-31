"""Hierarchical, cross-process shared/exclusive locks for file operations."""

from __future__ import annotations

import hashlib
import os
import threading
import time
import weakref
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Optional, Sequence

from mote.contracts.file.errors import FileLockCancelledError, FileLockTimeoutError
from mote.contracts.file.identity import LockMode, LockSpec

if os.name == "posix":
    from mote.runtime.fileops._posix_lock import try_os_lock as _try_os_lock
    from mote.runtime.fileops._posix_lock import unlock_os as _unlock_os
elif os.name == "nt":  # pragma: no cover - exercised on Windows CI
    from mote.runtime.fileops._windows_lock import try_os_lock as _try_os_lock
    from mote.runtime.fileops._windows_lock import unlock_os as _unlock_os
else:  # pragma: no cover - construction fails before these are called

    def _try_os_lock(fd: int, mode: LockMode) -> bool:
        raise OSError("platform has no supported cross-process file lock backend")

    def _unlock_os(fd: int) -> None:
        raise OSError("platform has no supported cross-process file lock backend")


TIMELINE_LOCK_LEVEL = -1
PROJECT_LOCK_LEVEL = 0
NAME_LOCK_LEVEL = 1
TARGET_LOCK_LEVEL = 2
JOURNAL_LOCK_LEVEL = 3
ARTIFACT_LOCK_LEVEL = 4

_POLL_INTERVAL = 0.02


class _LocalToken:
    def __init__(self, lock: "_LocalRWLock", mode: LockMode, first: bool) -> None:
        self.lock = lock
        self.mode = mode
        self.first = first


class _LocalRWLock:
    """Writer-fair, per-thread reentrant RW lock."""

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._readers: dict[int, int] = {}
        self._writer: Optional[int] = None
        self._writer_depth = 0
        self._waiting_writers = 0

    def acquire(
        self,
        mode: LockMode,
        *,
        deadline: Optional[float],
        cancel: Optional[threading.Event],
        label: str,
    ) -> _LocalToken:
        thread_id = threading.get_ident()
        with self._condition:
            if mode == LockMode.SHARED:
                if self._writer == thread_id:
                    return _LocalToken(self, mode, first=False)
                prior = self._readers.get(thread_id, 0)
                while self._writer is not None or (self._waiting_writers > 0 and prior == 0):
                    self._wait(deadline, cancel, label)
                self._readers[thread_id] = prior + 1
                return _LocalToken(self, mode, first=prior == 0)

            if self._writer == thread_id:
                self._writer_depth += 1
                return _LocalToken(self, mode, first=False)
            if self._readers.get(thread_id, 0):
                raise RuntimeError("lock upgrade from shared to exclusive is forbidden")
            self._waiting_writers += 1
            try:
                while self._writer is not None or self._readers:
                    self._wait(deadline, cancel, label)
                self._writer = thread_id
                self._writer_depth = 1
                return _LocalToken(self, mode, first=True)
            finally:
                self._waiting_writers -= 1

    def release(self, token: _LocalToken) -> None:
        thread_id = threading.get_ident()
        with self._condition:
            if token.mode == LockMode.SHARED:
                if self._writer == thread_id and not token.first:
                    return
                depth = self._readers.get(thread_id, 0)
                if depth <= 0:
                    raise RuntimeError("shared lock released by non-owner")
                if depth == 1:
                    del self._readers[thread_id]
                else:
                    self._readers[thread_id] = depth - 1
            else:
                if self._writer != thread_id or self._writer_depth <= 0:
                    raise RuntimeError("exclusive lock released by non-owner")
                self._writer_depth -= 1
                if self._writer_depth == 0:
                    self._writer = None
            self._condition.notify_all()

    def _wait(
        self,
        deadline: Optional[float],
        cancel: Optional[threading.Event],
        label: str,
    ) -> None:
        if cancel is not None and cancel.is_set():
            raise FileLockCancelledError(f"lock wait cancelled: {label}", lock=label)
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise FileLockTimeoutError(f"timed out waiting for lock: {label}", lock=label)
        self._condition.wait(_POLL_INTERVAL if remaining is None else min(_POLL_INTERVAL, remaining))


_REGISTRY_GUARD = threading.RLock()
_REGISTRY: "weakref.WeakValueDictionary[str, _LocalRWLock]" = weakref.WeakValueDictionary()


def _local_lock(registry_key: str) -> _LocalRWLock:
    with _REGISTRY_GUARD:
        lock = _REGISTRY.get(registry_key)
        if lock is None:
            lock = _LocalRWLock()
            _REGISTRY[registry_key] = lock
        return lock


class _HeldLock:
    def __init__(self, local_token: _LocalToken, fd: Optional[int]) -> None:
        self.local_token = local_token
        self.fd = fd

    def release(self) -> None:
        if self.fd is not None:
            try:
                _unlock_os(self.fd)
            finally:
                os.close(self.fd)
        self.local_token.lock.release(self.local_token)


class _LockSetLease(AbstractContextManager[None]):
    def __init__(
        self,
        manager: "HierarchicalLockManager",
        specs: Sequence[LockSpec],
        timeout: Optional[float],
        cancel: Optional[threading.Event],
    ) -> None:
        self._manager = manager
        self._specs = manager._normalize(specs)
        self._timeout = timeout
        self._cancel = cancel
        self._held: list[_HeldLock] = []

    def __enter__(self) -> None:
        deadline = None if self._timeout is None else time.monotonic() + max(0.0, self._timeout)
        try:
            for spec in self._specs:
                self._held.append(self._manager._acquire_one(spec, deadline=deadline, cancel=self._cancel))
        except Exception:
            self._release_all()
            raise
        return None

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._release_all()
        return None

    def _release_all(self) -> None:
        first_error: Optional[BaseException] = None
        while self._held:
            try:
                self._held.pop().release()
            except BaseException as exc:  # lock release must continue for the remaining set
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error


class HierarchicalLockManager:
    """Acquires sorted project/name/target lock sets with one deadline."""

    def __init__(self, root: Path) -> None:
        if os.name not in {"posix", "nt"}:
            raise OSError("platform has no supported cross-process file lock backend")
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            os.chmod(self._root, 0o700)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def backend_name(self) -> str:
        if os.name == "posix":
            return "posix-flock"
        if os.name == "nt":
            return "windows-lockfileex"
        raise OSError("platform has no supported cross-process file lock backend")

    def acquire_many(
        self,
        specs: Sequence[LockSpec],
        *,
        timeout: Optional[float] = None,
        cancel: Optional[threading.Event] = None,
    ) -> _LockSetLease:
        return _LockSetLease(self, specs, timeout, cancel)

    def _acquire_one(
        self,
        spec: LockSpec,
        *,
        deadline: Optional[float],
        cancel: Optional[threading.Event],
    ) -> _HeldLock:
        lock_path = self._lock_path(spec)
        registry_key = str(lock_path)
        local = _local_lock(registry_key)
        token = local.acquire(spec.mode, deadline=deadline, cancel=cancel, label=spec.label or spec.key)
        if not token.first:
            return _HeldLock(token, None)

        fd: Optional[int] = None
        try:
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            if os.name == "posix":
                os.fchmod(fd, 0o600)
            while not _try_os_lock(fd, spec.mode):
                if cancel is not None and cancel.is_set():
                    raise FileLockCancelledError(
                        f"lock wait cancelled: {spec.label or spec.key}",
                        lock_key=spec.key,
                    )
                if deadline is not None and time.monotonic() >= deadline:
                    raise FileLockTimeoutError(
                        f"timed out waiting for lock: {spec.label or spec.key}",
                        lock_key=spec.key,
                    )
                time.sleep(_POLL_INTERVAL)
            return _HeldLock(token, fd)
        except Exception:
            if fd is not None:
                os.close(fd)
            local.release(token)
            raise

    def _lock_path(self, spec: LockSpec) -> Path:
        category = {
            TIMELINE_LOCK_LEVEL: "timelines",
            PROJECT_LOCK_LEVEL: "projects",
            NAME_LOCK_LEVEL: "names",
            TARGET_LOCK_LEVEL: "targets",
            JOURNAL_LOCK_LEVEL: "journals",
            ARTIFACT_LOCK_LEVEL: "artifacts",
        }.get(spec.level)
        if category is None:
            raise ValueError(f"invalid file lock level: {spec.level}")
        directory = self._root / category
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            os.chmod(directory, 0o700)
        filename = hashlib.sha256(f"{spec.level}\0{spec.key}".encode("utf-8")).hexdigest() + ".lock"
        return directory / filename

    @staticmethod
    def _normalize(specs: Sequence[LockSpec]) -> tuple[LockSpec, ...]:
        merged: dict[tuple[int, str], LockSpec] = {}
        for spec in specs:
            identity = (spec.level, spec.key)
            prior = merged.get(identity)
            if prior is None or spec.mode == LockMode.EXCLUSIVE:
                merged[identity] = spec
        return tuple(sorted(merged.values(), key=lambda item: (item.level, item.key)))


__all__ = [
    "ARTIFACT_LOCK_LEVEL",
    "HierarchicalLockManager",
    "JOURNAL_LOCK_LEVEL",
    "NAME_LOCK_LEVEL",
    "PROJECT_LOCK_LEVEL",
    "TARGET_LOCK_LEVEL",
    "TIMELINE_LOCK_LEVEL",
]
