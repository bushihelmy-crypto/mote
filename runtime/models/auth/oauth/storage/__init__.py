#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Credential storage backends + ``get_store`` factory."""

from __future__ import annotations

from pathlib import Path
from typing import Union

from mote.contracts.config.model.oauth import StoreBackend
from mote.runtime.models.auth.oauth.storage.base import CredentialStore
from mote.runtime.models.auth.oauth.storage.file_store import FileCredentialStore
from mote.runtime.models.auth.oauth.storage.keyring_store import KeyringCredentialStore


def get_store(
    provider: str,
    backend: Union[StoreBackend, str],
    *,
    base_dir: Path,
) -> CredentialStore:
    """Return a :class:`CredentialStore` for ``provider`` using ``backend``.

    Backend selection is explicit and stable for the credential subject.
    """
    backend = StoreBackend(backend) if not isinstance(backend, StoreBackend) else backend
    if backend == StoreBackend.FILE:
        return FileCredentialStore(provider, base_dir)
    if backend == StoreBackend.KEYRING:
        return KeyringCredentialStore(provider, base_dir)
    raise ValueError(f"unsupported OAuth credential backend: {backend!r}")


__all__ = [
    "CredentialStore",
    "FileCredentialStore",
    "get_store",
]
