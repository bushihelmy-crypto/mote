#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Optional OS-keyring-backed credential store.

Uses the ``keyring`` package when installed. Construction raises if ``keyring``
is unavailable so the fallback chain can degrade to the file store.
"""
from __future__ import annotations

import json
from typing import Optional

try:
    import keyring
except Exception as _keyring_import_error:  # noqa: BLE001 — optional backend
    keyring = None
else:
    _keyring_import_error = None

from mote.runtime.models.auth.oauth.models import OAuthToken
from mote.runtime.models.auth.oauth.storage.base import CredentialStore
from mote.runtime.telemetry.logging import logger

_SERVICE = "mote-oauth"


class KeyringCredentialStore(CredentialStore):
    """Stores the token JSON as a single keyring secret keyed by provider."""

    def __init__(self, provider: str) -> None:
        super().__init__(provider)
        if keyring is None:
            raise RuntimeError(f"keyring backend unavailable: {_keyring_import_error}")
        self._keyring = keyring

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
