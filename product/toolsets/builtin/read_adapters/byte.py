"""Raw and hexadecimal byte-view formatting for Read."""

from __future__ import annotations

import base64

from mote.contracts.file import ByteViewMode, FileByteView


def format_byte_view(file_path: str, view: FileByteView) -> tuple[str, dict]:
    """Format a sealed byte view and its model-facing result metadata."""
    if view.mode == ByteViewMode.RAW:
        payload = base64.b64encode(view.data).decode("ascii")
        body = f"base64:{payload}"
        encoding = "base64"
    else:
        body = view.text or "<empty byte range>"
        encoding = "hex"
    next_hint = (
        f"; next_offset={view.next_offset}; next_cursor={view.next_cursor}" if view.next_offset is not None else ""
    )
    output = (
        f"{view.mode.value} view of {file_path}: offset={view.offset}; "
        f"returned={len(view.data)} bytes; total={view.total_bytes}; "
        f"status={view.status.value}{next_hint}\n{body}"
    )
    return output, {
        "type": view.mode.value,
        "byte_offset": view.offset,
        "bytes_returned": len(view.data),
        "total_bytes": view.total_bytes,
        "status": view.status.value,
        "next_offset": view.next_offset,
        "next_cursor": view.next_cursor,
        "encoding": encoding,
        "snapshot_digest": view.snapshot.version.digest,
    }


__all__ = ["format_byte_view"]
