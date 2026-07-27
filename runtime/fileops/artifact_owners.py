"""Canonical non-sensitive owner identities for artifact reservations."""

from __future__ import annotations

import hashlib
import os


def artifact_owner(
    kind: str,
    identity: str | bytes | os.PathLike[str] | os.PathLike[bytes],
) -> str:
    if type(kind) is not str or not kind or not kind.isascii() or ":" in kind:
        raise ValueError("artifact owner kind must be non-empty canonical ASCII")
    if isinstance(identity, os.PathLike):
        identity = os.fspath(identity)
    if type(identity) not in (str, bytes):
        raise TypeError("artifact owner identity must be a path-like scalar")
    raw = identity if isinstance(identity, bytes) else os.fsencode(identity)
    return f"{kind}:{hashlib.sha256(raw).hexdigest()}"


__all__ = ["artifact_owner"]
