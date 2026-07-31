"""Windows cross-process file-lock backend."""

from __future__ import annotations

import ctypes
import msvcrt
from ctypes import wintypes

from mote.contracts.file.identity import LockMode

_LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
_LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
_ERROR_LOCK_VIOLATION = 33


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


def try_os_lock(fd: int, mode: LockMode) -> bool:
    flags = _LOCKFILE_FAIL_IMMEDIATELY
    if mode == LockMode.EXCLUSIVE:
        flags |= _LOCKFILE_EXCLUSIVE_LOCK
    overlapped = _OVERLAPPED()
    handle = msvcrt.get_osfhandle(fd)
    result = ctypes.windll.kernel32.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(overlapped))
    if result:
        return True
    error = ctypes.get_last_error()
    if error == _ERROR_LOCK_VIOLATION:
        return False
    raise ctypes.WinError(error)


def unlock_os(fd: int) -> None:
    overlapped = _OVERLAPPED()
    handle = msvcrt.get_osfhandle(fd)
    if not ctypes.windll.kernel32.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped)):
        raise ctypes.WinError(ctypes.get_last_error())


__all__ = ["try_os_lock", "unlock_os"]
