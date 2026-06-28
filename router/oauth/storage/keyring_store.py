#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Optional OS-keyring-backed credential store.

Uses the ``keyring`` package when installed. Construction raises if ``keyring``
is unavailable so the fallback chain can degrade to the file store.
"""
from __future__ import annotations

import json
from typing import Optional

from metagpt.common.logs import logger
from metagpt.router.oauth.models import OAuthToken
from metagpt.router.oauth.storage.base import CredentialStore

_SERVICE = "metagpt-oauth"


class KeyringCredentialStore(CredentialStore):
    """Stores the token JSON as a single keyring secret keyed by provider."""

    def __init__(self, provider: str) -> None:
        super().__init__(provider)
        try:
            import keyring  # noqa: F401
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"keyring backend unavailable: {e}") from e
        self._keyring = __import__("keyring")

    def load(self) -> Optional[OAuthToken]:
        raw = self._keyring.get_password(_SERVICE, self.provider)
        if not raw:
            return None
        try:
            return OAuthToken(**json.loads(raw))
        except Exception:  # noqa: BLE001
            return None

    def save(self, token: OAuthToken) -> None:
        self._keyring.set_password(_SERVICE, self.provider, json.dumps(token.model_dump()))

    def delete(self) -> None:
        try:
            self._keyring.delete_password(_SERVICE, self.provider)
        except Exception as exc:  # noqa: BLE001 - absent / backend error is non-fatal
            logger.debug(f"OAuth keyring delete failed for {self.provider}: {exc}")
