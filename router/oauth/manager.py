#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""OAuthManager: the single entry point the LLM client uses for tokens.

Owns ``(config, store, client)`` and serves a valid bearer token, refreshing
proactively within the configured expiry buffer. Uses a cross-process file lock
so concurrent processes/threads don't stampede the token endpoint, and re-reads
the store under the lock (mtime check) to skip a refresh another worker already
did.
"""
from __future__ import annotations

from typing import Optional

from metagpt.common.config.config.oauth_config import GrantType, OAuthProviderConfig
from metagpt.common.const import CONFIG_ROOT
from metagpt.common.logs import log_class
from metagpt.router.oauth.client import OAuthClient
from metagpt.router.oauth.errors import OAuthConfigError, OAuthRefreshError
from metagpt.router.oauth.models import OAuthToken
from metagpt.router.oauth.storage import CredentialStore, get_store

_LOCK_DIR = CONFIG_ROOT / "oauth"


@log_class(level="DEBUG")
class OAuthManager:
    """Serve and refresh OAuth bearer tokens for one provider."""

    def __init__(
        self,
        config: OAuthProviderConfig,
        *,
        provider: Optional[str] = None,
        store: Optional[CredentialStore] = None,
        client: Optional[OAuthClient] = None,
    ) -> None:
        self.config = config
        # A stable provider key derives the store filename + lock path.
        self.provider = provider or (config.client_id or "default")
        self._store = store if store is not None else get_store(self.provider, config.store_backend)
        self._client = client if client is not None else OAuthClient(config)
        self._cached: Optional[OAuthToken] = None
        self._lock_path = _LOCK_DIR / f"{self.provider}.lock"

    # --- public API --------------------------------------------------------

    def get_valid_token(self) -> str:
        """Return a non-expired bearer access token, refreshing if needed.

        Fast path: serve the memoized/stored token when it's outside the expiry
        buffer. Slow path: take the file lock, re-read the store (another worker
        may have just refreshed), and mint/refresh only if still expired.
        """
        buffer = self.config.expiry_buffer_s

        token = self._cached or self._store.load()
        if token is not None and not token.is_expired(buffer):
            self._cached = token
            return token.access_token

        return self._refresh_locked(buffer=buffer).access_token

    def force_refresh(self) -> Optional[OAuthToken]:
        """Mint/refresh ignoring the expiry buffer; persist and return token.

        Used by ``OpenAILLM.rotate_credential`` on 401/auth errors. Returns
        ``None`` when refresh permanently fails (caller treats as exhausted).
        """
        try:
            return self._refresh_locked(buffer=None, force=True)
        except OAuthRefreshError:
            return None

    # --- internals ---------------------------------------------------------

    def _refresh_locked(self, *, buffer: Optional[int], force: bool = False) -> OAuthToken:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        from filelock import FileLock

        with FileLock(str(self._lock_path)):
            # Cross-process re-read: another worker may have refreshed while we
            # waited for the lock. Skip redundant network refresh when valid.
            if not force:
                stored = self._store.load()
                if stored is not None and not stored.is_expired(buffer or 0):
                    self._cached = stored
                    return stored

            token = self._mint_or_refresh()
            self._store.save(token)
            self._cached = token
            return token

    def _mint_or_refresh(self) -> OAuthToken:
        """Decide between refresh and client_credentials based on available material."""
        # Prefer refreshing an existing/configured refresh token.
        existing = self._cached or self._store.load()
        refresh_token = (existing.refresh_token if existing else None) or self.config.refresh_token

        if refresh_token:
            return self._client.refresh(refresh_token)

        if self.config.grant_type == GrantType.REFRESH_TOKEN:
            raise OAuthConfigError(
                "grant_type=refresh_token but no refresh_token is configured or stored"
            )
        return self._client.client_credentials()
