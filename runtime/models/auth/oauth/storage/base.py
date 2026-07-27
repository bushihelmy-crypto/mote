#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CredentialStore ABC: persist/load/delete a single provider's OAuthToken."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from mote.runtime.models.auth.oauth.models import OAuthToken


class CredentialStore(ABC):
    """Persists exactly one :class:`OAuthToken` per provider key.

    Implementations must round-trip ``OAuthToken`` and return ``None`` when no
    token has been stored yet (first-token bootstrap path).
    """

    def __init__(self, provider: str) -> None:
        self.provider = provider

    @abstractmethod
    def load(self) -> Optional[OAuthToken]:
        """Return the stored token, or ``None`` if absent/unreadable."""

    @abstractmethod
    def save(self, token: OAuthToken) -> None:
        """Persist ``token`` (overwriting any existing one)."""

    @abstractmethod
    def delete(self) -> None:
        """Remove any stored token (idempotent)."""

    def mtime(self) -> Optional[float]:
        """Return the last-modified time of the backing store, if observable.

        Used by the manager for a cross-process staleness check. Backends that
        cannot observe mtime (e.g. keyring) return ``None``.
        """
        return None
