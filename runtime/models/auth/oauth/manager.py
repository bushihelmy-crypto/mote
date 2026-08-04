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

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from filelock import FileLock

from mote.contracts.config.model.oauth import GrantType, OAuthProviderConfig
from mote.runtime.models.auth.oauth.client import OAuthClient
from mote.runtime.models.auth.oauth.effects import OAuthEffectKind, OAuthEffectState, OAuthEffectStore
from mote.runtime.models.auth.oauth.errors import OAuthConfigError, OAuthRefreshError
from mote.runtime.models.auth.oauth.flows import LoginCallbacks, run_auth_code_flow, run_device_code_flow
from mote.runtime.models.auth.oauth.models import OAuthToken
from mote.runtime.models.auth.oauth.storage import CredentialStore, get_store
from mote.runtime.models.auth.oauth.storage.base import (
    CredentialAction,
    CredentialBorrow,
    CredentialCommand,
    CredentialCommandDisposition,
    CredentialCommandReceipt,
    CredentialState,
    CredentialUse,
)
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
        consumer_id: str,
        store: Optional[CredentialStore] = None,
        client: Optional[OAuthClient] = None,
    ) -> None:
        self.config = config
        # The external name is display/config identity only; stores derive a
        # fixed credential subject before constructing any durable path.
        self.provider = provider or (config.client_id or "default")
        self._use = CredentialUse(
            provider=self.provider,
            account=config.client_id or "default-account",
            scopes=tuple(config.scopes),
            consumer_id=consumer_id,
        )
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
        lock_root = Path(storage_root or getattr(self._store, "path", Path(".")).parent).resolve()
        self._lock_path = (lock_root / f"{self._store.subject}.lock").resolve()
        self._effects = OAuthEffectStore(lock_root / f"{self._store.subject}.effects.jsonl")
        if self._lock_path.parent != lock_root:
            raise ValueError("OAuth lock path escaped storage root")

    # --- public API --------------------------------------------------------

    def acquire_valid_borrow(self, *, expires_at: datetime) -> CredentialBorrow:
        """Acquire consumer-bound material after refresh settlement."""

        self._refresh_locked(buffer=self.config.expiry_buffer_s)
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self._lock_path)):
            borrowed = self._store.borrow(self._use, expires_at=expires_at)
            if borrowed is None or borrowed.metadata.state is not CredentialState.ACTIVE:
                raise OAuthRefreshError("OAuth credential borrow is unavailable", recoverable=False)
            return borrowed

    def release_borrow(self, borrow: CredentialBorrow) -> None:
        self._store.release_borrow(borrow)

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
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self._lock_path)):
            grant = self.config.grant_type
            if grant not in _INTERACTIVE_GRANTS:
                raise OAuthConfigError(f"login() requires an interactive grant_type; {grant.value!r} is headless")
            current = self._store.load_metadata()
            effect = self._effects.commit_intent(
                self._store.subject,
                OAuthEffectKind.LOGIN,
                0 if current is None else current.revision,
                0 if current is None else current.secret_generation,
            )
            if effect.state is not OAuthEffectState.INTENT_COMMITTED:
                raise RuntimeError("OAuth login effect requires owner reconciliation")
            try:
                if grant == GrantType.AUTHORIZATION_CODE:
                    token = run_auth_code_flow(self.config, callbacks)
                elif grant == GrantType.DEVICE_CODE:
                    token = run_device_code_flow(self.config, callbacks)
            except Exception as exc:
                self._effects.settle(effect.effect_id, OAuthEffectState.IN_DOUBT, type(exc).__name__)
                raise
            self._effects.settle(effect.effect_id, OAuthEffectState.SUCCEEDED, "provider-token-received")
            self._store.publish(token, expected_revision=current.revision if current is not None else 0)
            return token

    def execute(self, command: CredentialCommand) -> CredentialCommandReceipt:
        """Execute one typed operator command under the subject lock."""
        if command.subject != self._store.subject:
            raise ValueError("OAuth command targets another credential subject")
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self._lock_path)):
            borrowed = self._store.borrow(
                self._use,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
            )
            try:
                return self._execute_locked(command, borrowed)
            finally:
                if borrowed is not None:
                    self._store.release_borrow(borrowed)

    def _execute_locked(
        self,
        command: CredentialCommand,
        borrowed: CredentialBorrow | None,
    ) -> CredentialCommandReceipt:
        current = borrowed.metadata if borrowed is not None else self._store.load_metadata()
        if current is not None and current.revision != command.expected_revision:
            raise RuntimeError("OAuth command expectation is stale")
        if current is None:
            raise RuntimeError("OAuth command target is absent")
        if command.action in {CredentialAction.MIGRATE_BACKEND, CredentialAction.RESOLVE_CONFLICT}:
            return CredentialCommandReceipt(
                command.command_id,
                command.subject,
                command.action,
                current.state,
                current.revision,
                datetime.now(timezone.utc),
                CredentialCommandDisposition.REJECTED,
                "command requires the offline migration authority",
            )
        if command.action in {CredentialAction.APPLY_HOLD, CredentialAction.RELEASE_HOLD}:
            enabled = command.action is CredentialAction.APPLY_HOLD
            changed = current.legal_hold is not enabled
            final = self._store.set_legal_hold(enabled, expected_revision=current.revision)
            return CredentialCommandReceipt(
                command.command_id,
                command.subject,
                command.action,
                final.state,
                final.revision,
                datetime.now(timezone.utc),
                CredentialCommandDisposition.APPLIED if changed else CredentialCommandDisposition.ALREADY_APPLIED,
            )
        if command.action is CredentialAction.REVALIDATE_EXPIRY:
            return CredentialCommandReceipt(
                command.command_id,
                command.subject,
                command.action,
                current.state,
                current.revision,
                datetime.now(timezone.utc),
                CredentialCommandDisposition.ALREADY_APPLIED,
                "active material was validated by the generation-bound borrow",
            )
        if command.action in {
            CredentialAction.CRYPTO_ERASE,
            CredentialAction.RETIRE,
            CredentialAction.SECURITY_CLEAR,
        }:
            if command.action is not CredentialAction.SECURITY_CLEAR and current.secret_ref is not None:
                return CredentialCommandReceipt(
                    command.command_id,
                    command.subject,
                    command.action,
                    current.state,
                    current.revision,
                    datetime.now(timezone.utc),
                    CredentialCommandDisposition.REJECTED,
                    "provider logout must settle before ordinary erasure",
                )
            final = (
                current
                if current.state is CredentialState.RETIRED
                else self._store.transition(CredentialState.RETIRED, expected_revision=current.revision)
            )
            return CredentialCommandReceipt(
                command.command_id,
                command.subject,
                command.action,
                final.state,
                final.revision,
                datetime.now(timezone.utc),
                (
                    CredentialCommandDisposition.APPLIED
                    if final is not current
                    else CredentialCommandDisposition.ALREADY_APPLIED
                ),
            )
        if command.action is not CredentialAction.LOGOUT:
            raise ValueError("OAuth command action is unsupported")
        if current.state is not CredentialState.REVOKED:
            if borrowed is None:
                raise RuntimeError("OAuth revocation requires retained credential material")
            pending = self._store.transition(CredentialState.REVOCATION_PENDING, expected_revision=current.revision)
            effect = self._effects.commit_intent(
                self._store.subject,
                OAuthEffectKind.REVOKE,
                pending.revision,
                pending.secret_generation,
            )
            try:
                revoked = self._client.revoke(borrowed.token.refresh_token or borrowed.token.access_token)
            except Exception as exc:
                self._effects.settle(effect.effect_id, OAuthEffectState.IN_DOUBT, type(exc).__name__)
                self._store.transition(CredentialState.IN_DOUBT, expected_revision=pending.revision)
                raise
            state = OAuthEffectState.SUCCEEDED if revoked else OAuthEffectState.FAILED
            self._effects.settle(
                effect.effect_id,
                state,
                "provider-revoked" if revoked else "provider-rejected",
            )
            terminal = CredentialState.REVOKED if revoked else CredentialState.OWNER_ACTION_REQUIRED
            self._store.transition(terminal, expected_revision=pending.revision)
        final = self._store.load_metadata()
        if final is None:
            raise RuntimeError("OAuth revoke did not commit credential metadata")
        return CredentialCommandReceipt(
            command.command_id,
            command.subject,
            command.action,
            final.state,
            final.revision,
            datetime.now(timezone.utc),
        )

    # --- internals ---------------------------------------------------------

    def _refresh_locked(self, *, buffer: Optional[int], force: bool = False) -> OAuthToken:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self._lock_path)):
            # Cross-process re-read: another worker may have refreshed while we
            # waited for the lock. Skip redundant network refresh when valid.
            borrowed = self._store.borrow(
                self._use,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
            )
            try:
                return self._refresh_with_borrow(borrowed, buffer=buffer, force=force)
            finally:
                if borrowed is not None:
                    self._store.release_borrow(borrowed)

    def _refresh_with_borrow(
        self,
        borrowed: CredentialBorrow | None,
        *,
        buffer: Optional[int],
        force: bool,
    ) -> OAuthToken:
        current = borrowed.metadata if borrowed is not None else self._store.load_metadata()
        stored = borrowed.token if borrowed is not None else None
        if current is not None and current.state is not CredentialState.ACTIVE:
            raise OAuthRefreshError(
                f"OAuth credential requires owner action: {current.state.value}",
                recoverable=False,
            )
        if not force:
            if stored is not None and not stored.is_expired(buffer or 0):
                return stored

        kind = (
            OAuthEffectKind.REFRESH
            if ((stored and stored.refresh_token) or self.config.refresh_token)
            else OAuthEffectKind.LOGIN
        )
        revision = current.revision if current is not None else 0
        generation = current.secret_generation if current is not None else 0
        effect = self._effects.commit_intent(self._store.subject, kind, revision, generation)
        if effect.state is not OAuthEffectState.INTENT_COMMITTED:
            raise OAuthRefreshError("OAuth effect requires owner reconciliation", recoverable=False)
        try:
            token = self._mint_or_refresh(stored)
        except Exception as exc:
            self._effects.settle(effect.effect_id, OAuthEffectState.IN_DOUBT, type(exc).__name__)
            if current is not None:
                self._store.transition(CredentialState.IN_DOUBT, expected_revision=current.revision)
            raise
        self._effects.settle(effect.effect_id, OAuthEffectState.SUCCEEDED, "provider-token-received")
        try:
            self._store.publish(token, expected_revision=revision)
        except Exception:
            if current is not None:
                self._store.transition(
                    CredentialState.OWNER_ACTION_REQUIRED,
                    expected_revision=current.revision,
                )
            raise
        return token

    def _mint_or_refresh(self, existing: OAuthToken | None) -> OAuthToken:
        """Decide between refresh and client_credentials based on available material."""
        # Prefer refreshing an existing/configured refresh token.
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
