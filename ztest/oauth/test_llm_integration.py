#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for single-slot OAuth wiring in OpenAILLM.

We monkeypatch the OAuthManager imported by ``OpenAILLM._build_oauth_manager``
so no network/token-endpoint call happens.
"""
from __future__ import annotations

import mote.runtime.models.clients.credentials as cred_mod
from mote.contracts.config.model.llm import LLMConfig
from mote.contracts.config.model.oauth import OAuthProviderConfig
from mote.product.models.providers.openai_chat import OpenAILLM


class FakeManager:
    """Stand-in OAuthManager: serves a token and force-rotates to a new one."""

    def __init__(self, config):
        self.config = config
        self._n = 0

    def get_valid_token(self) -> str:
        return "tok-0" if self._n == 0 else f"tok-{self._n}"

    def force_refresh(self):
        self._n += 1

        class _T:
            access_token = f"tok-{self._n}"

        return _T()


def _oauth_cfg(**kw) -> OAuthProviderConfig:
    base = dict(token_url="https://issuer/token", client_id="cid")
    base.update(kw)
    return OAuthProviderConfig(**base)


def test_static_key_path_disables_sdk_retries():
    llm = OpenAILLM(LLMConfig(api_key="sk-static", base_url="https://api.example/v1"))
    assert llm._oauth is None
    kwargs = llm._make_client_kwargs()
    assert kwargs == {
        "api_key": "sk-static",
        "base_url": "https://api.example/v1",
        "max_retries": 0,
    }
    assert "default_headers" not in kwargs


def test_oauth_injects_token_as_api_key_and_headers(monkeypatch):
    monkeypatch.setattr(cred_mod, "OAuthManager", FakeManager)
    cfg = LLMConfig(
        api_key="",
        base_url="https://api.example/v1",
        oauth=_oauth_cfg(headers_extra={"X-Org": "acme"}),
    )
    llm = OpenAILLM(cfg)
    assert llm._oauth is not None
    kwargs = llm._make_client_kwargs()
    assert kwargs["api_key"] == "tok-0"
    assert kwargs["default_headers"] == {"X-Org": "acme"}


def test_product_controlled_refresh_rebuilds_oauth_client(monkeypatch):
    monkeypatch.setattr(cred_mod, "OAuthManager", FakeManager)
    cfg = LLMConfig(api_key="", oauth=_oauth_cfg())
    llm = OpenAILLM(cfg)
    assert llm._make_client_kwargs()["api_key"] == "tok-0"

    assert llm.refresh_oauth_credential() is True
    assert llm._make_client_kwargs()["api_key"] == "tok-1"


def test_provider_rejects_unresolved_static_key_pool():
    # Gateway/Resolver must select one opaque slot before provider construction.
    import pytest

    with pytest.raises(ValueError, match="one Product-selected credential slot"):
        OpenAILLM(LLMConfig(api_key=["k1", "k2"]))
