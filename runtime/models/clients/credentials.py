# -*- coding: utf-8 -*-
"""Single-slot credential material for Product-bound LLM providers.

Static-key selection belongs to the ModelGateway. OAuth refresh is exposed as a
capability that the Product endpoint adapter may invoke when the Gateway selects
the opaque refresh slot; providers never decide to rotate, retry, or fail over.

A provider mixes this in and implements :meth:`_rebuild_client` (build a fresh
SDK client from the current credential). It must call :meth:`_init_credentials`
during its client init, before the first :meth:`_rebuild_client`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mote.contracts.config.model.llm import LLMConfig


class CredentialBindingMixin:
    """Build one provider client from one Product-selected credential slot."""

    # Supplied by the concrete provider that mixes this in (declared here so the
    # shared rotation logic type-checks against it without importing the host).
    config: "LLMConfig"

    def _init_credentials(self) -> None:
        """Bind exactly one static key or one OAuth token source.

        Sets the single-key binding. OAuth belongs to the Product generation
        credential lease and is rejected on this direct provider path. Call from
        the provider's client init before the first :meth:`_rebuild_client`.
        """
        if self.config.oauth is not None:
            raise RuntimeError("OAuth model credentials require the Product generation credential lease")
        keys = self.config.api_key
        self._api_keys: list[str] = list(keys) if isinstance(keys, list) else [keys]
        if len(self._api_keys) != 1:
            raise ValueError("provider clients require one Product-selected credential slot")

    def _current_api_key(self) -> str:
        return self._api_keys[0]

    def _rebuild_client(self):
        """Build a fresh SDK client from the current credential. Provider-specific."""
        raise NotImplementedError

    def _replace_client(self, client) -> None:
        """Swap the active client and retain the old pool until ``aclose``."""

        previous = getattr(self, "aclient", None)
        if previous is not None and previous is not client:
            retired = getattr(self, "_retired_clients", None)
            if retired is None:
                retired = []
                self._retired_clients = retired
            retired.append(previous)
        self.aclient = client
