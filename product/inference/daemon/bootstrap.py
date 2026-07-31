"""One-shot inherited-FD bootstrap channel for Shared daemon secrets."""

from __future__ import annotations

import os
import socket
import struct

_MAX_BOOTSTRAP_BYTES = 1024 * 1024


def read_inherited_bootstrap() -> bytes:
    raw_fd = os.environ.pop("MOTE_SHARED_BOOTSTRAP_FD", None)
    if raw_fd is None or not raw_fd.isdecimal():
        raise RuntimeError("Shared bootstrap descriptor is unavailable")
    descriptor = int(raw_fd)
    channel = socket.socket(fileno=descriptor)
    try:
        size = struct.unpack(">I", _read_exact(channel, 4))[0]
        if size <= 0 or size > _MAX_BOOTSTRAP_BYTES:
            raise RuntimeError("Shared bootstrap payload size is invalid")
        payload = _read_exact(channel, size)
        if channel.recv(1):
            raise RuntimeError("Shared bootstrap channel has trailing data")
        return payload
    finally:
        channel.close()


def _read_exact(channel: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = channel.recv(remaining)
        if not chunk:
            raise RuntimeError("Shared bootstrap channel closed prematurely")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


__all__ = ["read_inherited_bootstrap"]
