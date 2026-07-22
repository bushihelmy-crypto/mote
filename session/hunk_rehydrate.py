"""Shared content-fetch helpers for hunk rehydration.

Both the write-side engine (:mod:`~mote.session.hunk_ops`) and the read-side
projection (:mod:`~mote.session.attribution`) reconstruct a hunk's text from the
same two sources: the OLD side from a before-image blob (keyed by ``pre_hash``)
and the NEW side from the live file on disk. These two tiny best-effort readers
are that shared fetch — homed here once rather than copied into each consumer.

Both are lossy-tolerant on purpose: a missing blob or an unreadable file yields
``""`` rather than raising, so a review UI can always render *something* (the
geometry + attribution are always present even when the content is gone).
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["blob_text", "read_current"]


def blob_text(blobs, digest: str) -> str:
    """Fetch and UTF-8 decode the blob at *digest*, or ``""`` when absent.

    ``blobs`` is any content-addressed store exposing ``get(digest) -> bytes``.
    An empty *digest* (a pure insertion has no before-image) or a missing blob
    both yield ``""``.
    """
    if not digest:
        return ""
    blob = blobs.get(digest)
    return blob.decode("utf-8", errors="replace") if blob is not None else ""


def read_current(path: str) -> str:
    """Read *path* as UTF-8 text, or ``""`` when it is missing/unreadable."""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
