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

from pathlib import Path
from typing import Optional

from filelock import FileLock

from mote.contracts.config.model.oauth import GrantType, OAuthProviderConfig
from mote.runtime.models.auth.oauth.client import OAuthClient
from mote.runtime.models.auth.oauth.errors import OAuthConfigError, OAuthRefreshError
from mote.runtime.models.auth.oauth.flows import LoginCallbacks, run_auth_code_flow, run_device_code_flow
from mote.runtime.models.auth.oauth.models import OAuthToken
from mote.runtime.models.auth.oauth.storage import CredentialStore, get_store
from mote.runtime.telemetry.logging import log_class

_INTERACTIVE_GRANTS = (GrantType.AUTHORIZATION_CODE, GrantType.DEVICE_CODE)


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
        storage_root = config.storage_root
        if store is None and storage_root is None:
            raise ValueError("OAuthManager requires an explicit credential storage root")
        if store is None:
            assert storage_root is not None
            store = get_store(
                self.provider,
                config.store_backend,
                base_dir=storage_root,
            )
        self._store = store
        self._client = client if client is not None else OAuthClient(config)
        self._cached: Optional[OAuthToken] = None
        lock_root = storage_root or getattr(self._store, "path", Path(".")).parent
        self._lock_path = lock_root / f"{self.provider}.lock"

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

        Used by the Product OAuth refresh-slot adapter after the Gateway selects
        that slot for an authentication failure. Returns ``None`` when refresh
        permanently fails (the adapter treats the slot as exhausted).
        """
        try:
            return self._refresh_locked(buffer=None, force=True)
        except OAuthRefreshError:
            return None

    def login(self, callbacks: Optional[LoginCallbacks] = None) -> OAuthToken:
        """Run the interactive login flow for this provider and persist the token.

        Dispatches by ``grant_type``: ``authorization_code`` (PKCE + loopback)
        or ``device_code`` (RFC 8628). Raises :class:`OAuthConfigError` for a
        headless grant type (which has no interactive login).
        """
        grant = self.config.grant_type
        if grant == GrantType.AUTHORIZATION_CODE:
            token = run_auth_code_flow(self.config, callbacks)
        elif grant == GrantType.DEVICE_CODE:
            token = run_device_code_flow(self.config, callbacks)
        else:
            raise OAuthConfigError(f"login() requires an interactive grant_type; {grant.value!r} is headless")

        self._store.save(token)
        self._cached = token
        return token

    # --- internals ---------------------------------------------------------

    def _refresh_locked(self, *, buffer: Optional[int], force: bool = False) -> OAuthToken:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
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
            raise OAuthConfigError("grant_type=refresh_token but no refresh_token is configured or stored")
        if self.config.grant_type in _INTERACTIVE_GRANTS:
            raise OAuthConfigError(
                f"grant_type={self.config.grant_type.value!r} has no stored token; "
                "run an interactive login first (OAuthManager.login)"
            )
        return self._client.client_credentials()
