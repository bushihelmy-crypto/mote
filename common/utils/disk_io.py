"""Stateless disk read/write primitives shared across the codebase.

These are the low-level file operations factored out of
``metagpt/tasks/disk_output.py`` so that other modules (e.g. the
context-manager's tool-result persistence) can reuse the exact same
seek/read/write logic instead of re-implementing it.

All functions here are synchronous and side-effect-local: they touch one file
path and return plain bytes / ints. Callers decide whether to wrap a call in
``asyncio.to_thread`` for non-blocking IO — these helpers never assume an event
loop.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Union

PathLike = Union[str, Path]


def file_size(path: PathLike) -> int:
    """Return the file's size in bytes, or 0 if it does not exist."""
    p = Path(path)
    return p.stat().st_size if p.exists() else 0


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


def write_bytes(path: PathLike, data: bytes, *, append: bool = True) -> int:
    """Write *data* to *path*. Returns the number of bytes written.

    ``append=True`` appends to (or creates) the file; ``append=False``
    truncates first.
    """
    mode = "ab" if append else "wb"
    with open(path, mode) as f:
        f.write(data)
    return len(data)


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
