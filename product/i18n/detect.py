#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Resolve a locale code from config + the process environment.

Precedence (highest first):

1. an explicit ``config.ui.language`` (``"en"`` / ``"zh"`` / …) — this is also
   how ``MOTE_UI__LANGUAGE=en`` arrives, since the env config layer folds it in;
2. otherwise (``"auto"`` / empty) the POSIX locale env vars
   (``LC_ALL`` → ``LC_MESSAGES`` → ``LANG``), stripped of encoding/modifier;
3. otherwise ``None`` → the caller's negotiation default.

We deliberately do NOT call :func:`locale.setlocale` — that mutates global C
number/format state unreliably. We only read env vars and hand a tag to our own
negotiation.
"""
from __future__ import annotations

import os
from typing import Mapping, Optional

_ENV_VARS = ("LC_ALL", "LC_MESSAGES", "LANG")
_NEUTRAL = {"", "c", "posix"}


def _from_env(environ: Mapping[str, str]) -> Optional[str]:
    """First meaningful POSIX locale env var → its language tag (``zh_CN.UTF-8`` → ``zh_CN``)."""
    for var in _ENV_VARS:
        raw = environ.get(var)
        if not raw:
            continue
        # Strip ``.UTF-8`` encoding and ``@modifier`` suffixes → the bare tag.
        tag = raw.split(".", 1)[0].split("@", 1)[0].strip()
        if tag.lower() not in _NEUTRAL:
            return tag
    return None


def resolve_locale_code(
    config_language: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """Resolve the requested locale tag from config then env (see module docstring).

    Returns a tag to negotiate (e.g. ``"en"`` / ``"zh_CN"``) or ``None`` when
    nothing was requested (auto + no env), leaving the choice to negotiation.
    """
    if config_language and config_language.strip().lower() != "auto":
        return config_language.strip()
    env = os.environ if environ is None else environ
    return _from_env(env)


__all__ = ["resolve_locale_code"]
