#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Provider preset registry for OAuth-authenticated providers.

The presets now live beside :class:`OAuthProviderConfig` in
``metagpt.common.config.config.oauth_config`` so the config validator can apply
them without a ``common -> router`` import cycle. This module re-exports those
names as the router-facing surface (``router -> common`` is the correct
dependency direction), keeping ``metagpt.router.oauth.registry`` import paths
stable for existing callers and tests.
"""
from __future__ import annotations

from metagpt.common.config.config.oauth_config import (
    PROVIDER_PRESETS,
    apply_preset,
    get_preset,
    list_presets,
)

__all__ = ["PROVIDER_PRESETS", "list_presets", "get_preset", "apply_preset"]
