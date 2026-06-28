#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Keyring -> file fallback credential store (Claude Code pattern).

Prefers the OS keyring for secrecy; transparently degrades to the file store
when keyring is unavailable or errors at runtime.
"""
from __future__ import annotations

from typing import Optional

from metagpt.common.logs import logger
from metagpt.router.oauth.models import OAuthToken
from metagpt.router.oauth.storage.base import CredentialStore
from metagpt.router.oauth.storage.file_store import FileCredentialStore
from metagpt.router.oauth.storage.keyring_store import KeyringCredentialStore


class FallbackCredentialStore(CredentialStore):
    """Try keyring first, then fall back to the file store on any failure."""

    def __init__(self, provider: str) -> None:
        super().__init__(provider)
        self._file = FileCredentialStore(provider)
        self._keyring: Optional[CredentialStore] = None
        try:

            self._keyring = KeyringCredentialStore(provider)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"OAuth keyring backend unavailable, using file store: {e}")

    def load(self) -> Optional[OAuthToken]:
        if self._keyring is not None:
            try:
                token = self._keyring.load()
                if token is not None:
                    return token
            except Exception as e:  # noqa: BLE001
                logger.debug(f"OAuth keyring load failed, falling back to file: {e}")
        return self._file.load()

    def save(self, token: OAuthToken) -> None:
        if self._keyring is not None:
            try:
                self._keyring.save(token)
                return
            except Exception as e:  # noqa: BLE001
                logger.debug(f"OAuth keyring save failed, falling back to file: {e}")
        self._file.save(token)

    def delete(self) -> None:
        if self._keyring is not None:
            try:
                self._keyring.delete()
            except Exception as e:  # noqa: BLE001
                logger.debug(f"OAuth keyring delete failed, falling back to file: {e}")
        self._file.delete()

    def mtime(self) -> Optional[float]:
        # Only the file backend exposes mtime; keyring returns None.
        return self._file.mtime()
