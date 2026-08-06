"""Stateless Runtime disk read/write primitives.

These are the low-level file operations factored out of
``mote/tasks/disk_output.py`` so that other modules (e.g. the
context-manager's tool-result persistence) can reuse the exact same
seek/read/write logic instead of re-implementing it.

All functions here are synchronous and side-effect-local: they touch one file
path and return plain bytes / ints. Callers decide whether to wrap a call in
``asyncio.to_thread`` for non-blocking IO — these helpers never assume an event
loop.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional, Union

PathLike = Union[str, Path]


def file_size(path: PathLike) -> int:
    """Return the file's size in bytes, or 0 if it does not exist."""
    p = Path(path)
    return p.stat().st_size if p.exists() else 0


def mtime_ns(path: PathLike) -> Optional[int]:
    """Return the file's mtime in integer nanoseconds, or ``None`` if unreadable.

    The freshness primitive for lazy re-parse / change detection: a missing or
    unstat-able path yields ``None`` (treated as "changed") rather than raising.
    """
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def mtime_seconds(path: PathLike) -> Optional[float]:
    """Return the file's mtime in float seconds, or ``None`` if unreadable.

    Seconds-unit sibling of :func:`mtime_ns`: for durable-store staleness checks
    that compare against a previously-cached ``st_mtime`` (seconds). Same
    ``None``-on-``OSError`` contract — a missing file is "no known mtime", not a raise.
    """
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def read_range(path: PathLike, offset: int, length: int) -> bytes:
    """Read up to *length* bytes starting at *offset*.

    Returns ``b""`` when *offset* is at/after EOF or *length* <= 0. The read is
    clamped to the file's current size, so the result may be shorter than
    *length*.
    """
    if length <= 0:
        return b""
    size = file_size(path)
    if offset >= size:
        return b""
    read_len = min(length, size - offset)
    with open(path, "rb") as f:
        f.seek(offset)
        return f.read(read_len)


def read_tail(path: PathLike, max_bytes: int) -> bytes:
    """Read the last *max_bytes* of the file."""
    if max_bytes <= 0:
        return b""
    size = file_size(path)
    if size == 0:
        return b""
    offset = max(0, size - max_bytes)
    return read_range(path, offset, size - offset)


def write_bytes(
    path: PathLike,
    data: bytes,
    *,
    append: bool = True,
    fsync: bool = False,
    mode: int | None = None,
) -> int:
    """Write *data* to *path*. Returns the number of bytes written.

    ``append=True`` appends to (or creates) the file; ``append=False``
    truncates first. ``fsync=True`` forces the bytes to durable storage before
    returning (the append/journal path uses this; bulk callers leave it off).
    """
    p = Path(path)
    existed = p.exists()
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    descriptor = os.open(p, flags, mode if mode is not None else 0o666)
    with os.fdopen(descriptor, "ab" if append else "wb") as f:
        f.write(data)
        f.flush()
        if fsync:
            os.fsync(f.fileno())
    if mode is not None:
        os.chmod(p, mode)
    if fsync and not existed:
        fsync_directory(p.parent)
    return len(data)


def append_line(
    path: PathLike,
    line: str,
    *,
    fsync: bool = True,
    mode: int | None = None,
) -> None:
    """Append a single text *line* (a trailing newline is added) to *path*.

    The append-only journal primitive: ``O_APPEND`` keeps concurrent appends
    atomic at the line level and leaves earlier lines intact on crash, and
    ``fsync`` (on by default here) makes the line durable before returning.
    """
    write_bytes(
        path,
        (line + "\n").encode("utf-8"),
        append=True,
        fsync=fsync,
        mode=mode,
    )


def fsync_directory(path: PathLike) -> None:
    """Durably commit directory-entry changes or raise ``OSError``.

    Callers that promise crash durability must not treat failure to persist a
    newly created or replaced filename as a successful commit.
    """
    dir_fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def atomic_write(
    path: PathLike,
    data: bytes,
    *,
    fsync: bool = True,
    mode: int | None = None,
) -> None:
    """Atomically replace *path* with *data* (tmp-write + ``os.replace``).

    Writes to a unique temp file in the same directory, flushes (and, when
    ``fsync`` is set, fsyncs) it, then ``os.replace``-s it into place. A
    concurrent reader therefore sees either the old file or the complete new
    one — never a half-written file. With ``fsync`` the parent directory is also
    fsynced (best-effort) so the rename itself survives a crash. ``mode``
    applies owner/security permissions before bytes are written and again after
    replacement.

    This is the single home for the ``tmp + fsync + replace`` pattern previously
    duplicated across durable artifact and task stores.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=p.parent)
    tmp = Path(raw_tmp)
    try:
        if mode is not None:
            os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as f:
            fd = -1
            f.write(data)
            f.flush()
            if fsync:
                os.fsync(f.fileno())
        os.replace(tmp, p)
        if mode is not None:
            os.chmod(p, mode)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    if fsync:
        fsync_directory(p.parent)


def write_capped(
    path: PathLike,
    data: bytes,
    max_bytes: int,
    *,
    current_size: int,
    append: bool = True,
    cap_notice: bytes = b"",
) -> tuple[int, bool]:
    """Append *data* but never let the file grow past *max_bytes*.

    Args:
        path: Target file.
        data: Bytes to write.
        max_bytes: Hard cap on total file size (counted via *current_size*).
        current_size: Bytes already accounted for by the caller (the running
            total it maintains — not necessarily the on-disk size, which lets
            the caller keep an authoritative counter).
        append: Append when True, truncate-then-write when False.
        cap_notice: Optional marker bytes appended once when the cap is hit.

    Returns:
        ``(written, capped)`` where *written* counts only the real payload
        bytes that landed on disk (excluding *cap_notice*), and *capped* is
        True when the write was clamped by the cap.
    """
    new_total = current_size + len(data)
    capped = False
    if new_total > max_bytes:
        allowed = max(0, max_bytes - current_size)
        payload = data[:allowed] + cap_notice if allowed > 0 else cap_notice
        written = allowed
        capped = True
    else:
        payload = data
        written = len(data)

    if payload:
        write_bytes(path, payload, append=append)
    return written, capped


def truncate_file(path: PathLike) -> None:
    """Create the file if missing, or empty it if it exists."""
    Path(path).write_bytes(b"")


def remove_file(path: PathLike) -> None:
    """Delete the file, ignoring a missing path."""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def remove_tree(path: PathLike) -> bool:
    """Recursively delete a directory tree; return whether anything was removed.

    The bulk-removal counterpart to :func:`remove_file`, used by the workspace
    cleanup sweep to drop an expired session directory (or a legacy artifact
    tree) in one call. A missing path is not an error — it returns ``False``.
    Best-effort: a partial-failure mid-walk is logged by the caller, never
    raised here, so one unremovable entry can't abort a whole sweep.
    """
    p = Path(path)
    if not p.exists():
        return False
    shutil.rmtree(p, ignore_errors=True)
    return True
