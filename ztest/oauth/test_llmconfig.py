#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for oauth-aware LLMConfig key validation."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from metagpt.common.config.config.llm_config import LLMConfig
from metagpt.common.config.config.oauth_config import OAuthProviderConfig
from metagpt.common.exception import MissingAPIKeyError


def _oauth() -> OAuthProviderConfig:
    return OAuthProviderConfig(token_url="https://issuer/token", client_id="cid")


def test_static_key_still_required_without_oauth():
    with pytest.raises((MissingAPIKeyError, ValidationError)):
        LLMConfig(api_key="")


def test_placeholder_key_rejected_without_oauth():
    with pytest.raises((MissingAPIKeyError, ValidationError)):
        LLMConfig(api_key="YOUR_API_KEY")


def test_valid_static_key_ok():
    cfg = LLMConfig(api_key="sk-real")
    assert cfg.oauth is None


def test_oauth_allows_empty_api_key():
    cfg = LLMConfig(api_key="", oauth=_oauth())
    assert cfg.oauth is not None


def test_oauth_allows_placeholder_api_key():
    cfg = LLMConfig(api_key="YOUR_API_KEY", oauth=_oauth())
    assert cfg.oauth is not None


def test_nested_oauth_parses_from_dict():
    cfg = LLMConfig(
        api_key="",
        oauth={"token_url": "https://issuer/token", "client_id": "cid", "scopes": ["s1"]},
    )
    assert cfg.oauth.client_id == "cid"
    assert cfg.oauth.scopes == ["s1"]
