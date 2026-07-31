#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Provider catalog: data-only brand presets for LLM configuration.

The catalog now lives beside :class:`LLMConfig` in
``mote.contracts.config.model.llm`` so the config validator can apply
brand presets without a ``common -> router`` import cycle. This module re-exports
those names as the router-facing surface (``router -> common`` is the correct
dependency direction), with Runtime owning provider configuration lookup
paths stable for existing callers and tests.
"""
from __future__ import annotations

from mote.contracts.config.model.llm import (
    PROVIDER_CATALOG,
    ProviderPreset,
    apply_provider_preset,
    detect_provider,
    find_env_keys,
    get_env_api_key,
    get_provider_preset,
    list_providers,
    resolve_provider_name,
)

__all__ = [
    "ProviderPreset",
    "PROVIDER_CATALOG",
    "list_providers",
    "get_provider_preset",
    "resolve_provider_name",
    "apply_provider_preset",
    "detect_provider",
    "find_env_keys",
    "get_env_api_key",
]
