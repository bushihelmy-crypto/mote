"""Shared content-fetch helpers for hunk rehydration.

Both the write-side engine (:mod:`~mote.runtime.session.hunk_ops`) and the read-side
projection (:mod:`~mote.runtime.session.attribution`) reconstruct a hunk's text from the
same two sources: the OLD side from a before-image blob (keyed by ``pre_hash``)
and the NEW side from the live file on disk. This strict reader is the shared
fetch seam, homed here once rather than copied into each consumer. Missing or
corrupt durable review artifacts fail closed.
"""

from __future__ import annotations

from mote.runtime.fileops.mutation import FileMutationArtifactRepository

__all__ = ["blob_text"]


def blob_text(blobs: FileMutationArtifactRepository, digest: str) -> str:
    """Resolve and UTF-8 decode one live review artifact.

    An empty *digest* represents a pure insertion and yields ``""``.
    """
    if not digest:
        return ""
    artifact = blobs.resolve_live(digest)
    return blobs.read_bytes(artifact).decode("utf-8", errors="strict")
