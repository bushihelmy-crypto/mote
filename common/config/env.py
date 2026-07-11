#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Environment-variable config layer.

Maps ``MOTE_*`` / ``MOTE_*`` env vars into a nested override dict for
the ENV layer. ``__`` separates nesting levels while a single ``_`` stays
within a key segment, so ``MOTE_LLM__BASE_URL=...`` becomes
``{"llm": {"base_url": ...}}``. Segments are lower-cased; values are YAML-parsed
(``true``/``8000``/...) with a string fallback.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Mapping, Optional

from mote.common.config.overrides import parse_override_value, set_nested

ENV_PREFIXES = ("MOTE_",)


def build_env_layer(environ: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Build the nested ENV override dict from the process (or given) environment."""
    environ = os.environ if environ is None else environ
    data: Dict[str, Any] = {}
    for key, raw in environ.items():
        for prefix in ENV_PREFIXES:
            if key.startswith(prefix) and len(key) > len(prefix):
                segments = [s.lower() for s in key[len(prefix) :].split("__") if s]
                if segments:
                    set_nested(data, segments, parse_override_value(raw))
                break
    return data
