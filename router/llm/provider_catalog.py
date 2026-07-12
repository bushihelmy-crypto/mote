#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Provider catalog: data-only brand presets for LLM configuration.

The catalog now lives beside :class:`LLMConfig` in
``metagpt.common.config.config.llm_config`` so the config validator can apply
brand presets without a ``common -> router`` import cycle. This module re-exports
those names as the router-facing surface (``router -> common`` is the correct
dependency direction), keeping ``metagpt.router.llm.provider_catalog`` import
paths stable for existing callers and tests.
"""
from __future__ import annotations

from metagpt.common.config.config.llm_config import (
    PROVIDER_CATALOG,
    ProviderPreset,
    apply_provider_preset,
    find_env_keys,
    get_env_api_key,
    get_provider_preset,
    list_providers,
)

__all__ = [
    "ProviderPreset",
    "PROVIDER_CATALOG",
    "list_providers",
    "get_provider_preset",
    "apply_provider_preset",
    "find_env_keys",
    "get_env_api_key",
]
