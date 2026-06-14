#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Credential storage backends + ``get_store`` factory."""
from __future__ import annotations

from typing import Union

from metagpt.common.config.config.oauth_config import StoreBackend
from metagpt.router.oauth.storage.base import CredentialStore
from metagpt.router.oauth.storage.fallback_store import FallbackCredentialStore
from metagpt.router.oauth.storage.file_store import FileCredentialStore


def get_store(provider: str, backend: Union[StoreBackend, str] = StoreBackend.FALLBACK) -> CredentialStore:
    """Return a :class:`CredentialStore` for ``provider`` using ``backend``.

    ``file`` -> file store, ``keyring`` -> keyring store, ``fallback`` (default)
    -> keyring-then-file chain.
    """
    backend = StoreBackend(backend) if not isinstance(backend, StoreBackend) else backend
    if backend == StoreBackend.FILE:
        return FileCredentialStore(provider)
    if backend == StoreBackend.KEYRING:
        from metagpt.router.oauth.storage.keyring_store import KeyringCredentialStore

        return KeyringCredentialStore(provider)
    return FallbackCredentialStore(provider)


__all__ = ["CredentialStore", "FileCredentialStore", "FallbackCredentialStore", "get_store"]
