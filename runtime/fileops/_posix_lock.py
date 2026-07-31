"""POSIX cross-process file-lock backend."""

from __future__ import annotations

import fcntl

from mote.contracts.file.identity import LockMode


def try_os_lock(fd: int, mode: LockMode) -> bool:
    operation = fcntl.LOCK_SH if mode == LockMode.SHARED else fcntl.LOCK_EX
    try:
        fcntl.flock(fd, operation | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def unlock_os(fd: int) -> None:
    fcntl.flock(fd, fcntl.LOCK_UN)


__all__ = ["try_os_lock", "unlock_os"]
