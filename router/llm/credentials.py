# -*- coding: utf-8 -*-
"""Shared API-credential handling for LLM providers.

OpenAI- and Anthropic-backed providers normalize ``LLMConfig.api_key`` into a
rotatable list (or defer to an OAuth manager) and advance through it on the
recovery loop's ROTATE_CREDENTIAL action. That logic is identical across
transports — only the concrete SDK client differs — so it lives here as a mixin
rather than being copy-pasted per provider.

A provider mixes this in and implements :meth:`_rebuild_client` (build a fresh
SDK client from the current credential). It must call :meth:`_init_credentials`
during its client init, before the first :meth:`_rebuild_client`.
"""
from __future__ import annotations
from metagpt.router.oauth import OAuthManager


class CredentialRotationMixin:
    """Multi-key / OAuth credential rotation shared by LLM providers."""

    def _init_credentials(self) -> None:
        """Normalize ``config.api_key`` into a rotatable list and build OAuth (if any).

        Sets ``_api_keys`` / ``_api_key_index`` / ``_oauth``. Call from the
        provider's client init, before the first :meth:`_rebuild_client`.
        """
        keys = self.config.api_key
        self._api_keys: list[str] = list(keys) if isinstance(keys, list) else [keys]
        self._api_key_index: int = 0
        self._oauth = self._build_oauth_manager()

    def _build_oauth_manager(self):
        """Construct an OAuthManager when ``config.oauth`` is set, else None."""
        if not getattr(self.config, "oauth", None):
            return None

        return OAuthManager(self.config.oauth)

    def _current_api_key(self) -> str:
        return self._api_keys[self._api_key_index]

    def _rebuild_client(self):
        """Build a fresh SDK client from the current credential. Provider-specific."""
        raise NotImplementedError

    def rotate_credential(self) -> bool:
        """Advance to the next API key (or force-refresh OAuth) and rebuild the client.

        Consumed by the recovery loop on ROTATE_CREDENTIAL (auth/billing errors).
        Returns False when no further credential remains (rotation exhausted). In
        OAuth mode, rotation means force-refreshing the bearer token: a new token
        rebuilds the client and returns True; a permanently failed refresh → False.
        """
        if self._oauth is not None:
            token = self._oauth.force_refresh()
            if token is None:
                return False
            self.aclient = self._rebuild_client()
            return True
        if self._api_key_index + 1 >= len(self._api_keys):
            return False
        self._api_key_index += 1
        self.aclient = self._rebuild_client()
        return True
