#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Direct SDK providers cannot bypass Product-owned OAuth credential leases."""

from __future__ import annotations

import pytest

from mote.contracts.config.model.llm import LLMConfig
from mote.contracts.config.model.oauth import OAuthProviderConfig
from mote.product.models.providers.openai_chat import OpenAILLM


def _oauth_cfg(**kw) -> OAuthProviderConfig:
    base = dict(token_url="https://issuer/token", client_id="cid")
    base.update(kw)
    return OAuthProviderConfig(**base)


def test_static_key_path_disables_sdk_retries():
    llm = OpenAILLM(LLMConfig(api_key="sk-static", base_url="https://api.example/v1"))
    kwargs = llm._make_client_kwargs()
    assert kwargs == {
        "api_key": "sk-static",
        "base_url": "https://api.example/v1",
        "max_retries": 0,
    }
    assert "default_headers" not in kwargs


def test_direct_provider_rejects_oauth_bypass():
    cfg = LLMConfig(
        api_key="",
        base_url="https://api.example/v1",
        oauth=_oauth_cfg(headers_extra={"X-Org": "acme"}),
    )
    with pytest.raises(RuntimeError, match="Product generation credential lease"):
        OpenAILLM(cfg)


def test_provider_rejects_unresolved_static_key_pool():
    # Gateway/Resolver must select one opaque slot before provider construction.
    with pytest.raises(ValueError, match="one Product-selected credential slot"):
        OpenAILLM(LLMConfig(api_key=["k1", "k2"]))
