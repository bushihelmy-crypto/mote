#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Secret resolution: fetch the API key from an external helper command.

Keeping long-lived secrets out of config files is best practice (codex stores
them in ``auth.json``/keyring; claude-code resolves env > ``apiKeyHelper`` >
keychain). This module implements the helper-script path: when ``llm.api_key``
is absent or a placeholder *and* an ``api_key_helper`` command is configured,
the command is run and its stdout becomes the key.

Precedence is therefore: env (already merged into ``llm.api_key``) > static
config key > helper output. The helper only runs to fill a gap, never to
override an explicit key. Results are cached per-command with a short TTL so a
key is not re-fetched on every load.

Security: ``api_key_helper`` is on :data:`CREDENTIAL_DENYLIST`, so an untrusted
working-dir layer can never inject the command (it would be arbitrary RCE).
"""
from __future__ import annotations

import subprocess
import time
from typing import Any, Dict, Optional, Tuple

# Values that mean "no real key yet", so the helper should fill in.
_PLACEHOLDER_KEYS = frozenset({"", "sk-", "YOUR_API_KEY"})

# command -> (expires_at, key); avoids re-running the helper on every load.
_HELPER_CACHE: Dict[str, Tuple[float, str]] = {}
_HELPER_TTL_SECONDS = 300.0
_HELPER_TIMEOUT_SECONDS = 30.0


def _now() -> float:
    return time.monotonic()


def _needs_fill(api_key: Any) -> bool:
    """True when the current key is missing/placeholder and should be fetched."""
    if api_key is None:
        return True
    keys = api_key if isinstance(api_key, list) else [api_key]
    if not keys:
        return True
    # If every configured key is a placeholder, the helper should provide one.
    return all((k is None or k in _PLACEHOLDER_KEYS) for k in keys)


def run_api_key_helper(command: str, *, use_cache: bool = True) -> str:
    """Run ``command`` in a shell and return its stripped stdout as the key.

    Cached per-command for :data:`_HELPER_TTL_SECONDS`. A non-zero exit or empty
    output yields ``""`` (the caller then leaves the existing key in place).
    """
    command = command.strip()
    if not command:
        return ""
    if use_cache:
        cached = _HELPER_CACHE.get(command)
        if cached is not None and cached[0] > _now():
            return cached[1]
    try:
        completed = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=_HELPER_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    key = completed.stdout.strip() if completed.returncode == 0 else ""
    if key and use_cache:
        _HELPER_CACHE[command] = (_now() + _HELPER_TTL_SECONDS, key)
    return key


def resolve_api_key(merged: Dict[str, Any], *, use_cache: bool = True) -> Optional[str]:
    """Fill ``merged['llm']['api_key']`` from the helper when needed (in place).

    No-op unless a top-level ``api_key_helper`` is configured and the current
    ``llm.api_key`` is missing/placeholder. Returns the fetched key (or ``None``
    if nothing changed) for diagnostics/testing.
    """
    helper = merged.get("api_key_helper")
    if not helper or not isinstance(helper, str):
        return None
    llm = merged.get("llm")
    current = llm.get("api_key") if isinstance(llm, dict) else None
    if not _needs_fill(current):
        return None
    key = run_api_key_helper(helper, use_cache=use_cache)
    if not key:
        return None
    if not isinstance(llm, dict):
        llm = {}
        merged["llm"] = llm
    llm["api_key"] = key
    return key


def clear_cache() -> None:
    """Drop the helper-output cache (test hook / forced re-fetch)."""
    _HELPER_CACHE.clear()
