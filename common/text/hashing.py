"""Stable content fingerprinting for text.

The code-map extractor (which persists a per-file ``content_hash``) and the
whole-repo indexer (which recomputes hashes to diff for staleness) MUST agree on
the exact hashing recipe — otherwise every scan would see every file as stale.
That agreement used to live in a comment ("Must match CodeMapExtractor's hash
exactly"); homing it in one function makes the contract code-enforced instead.
"""
from __future__ import annotations

import hashlib


def content_hash(text: str) -> str:
    """Return the sha256 hex digest of *text* encoded as UTF-8.

    The canonical fingerprint for a file's textual content. Callers that read a
    file as UTF-8 text and want a stable version marker should route through here
    so every producer/consumer of the hash uses byte-identical inputs.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = ["content_hash"]
