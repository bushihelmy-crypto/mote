#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Product secret resolution and vault configuration."""
import subprocess
import time
from typing import Any, Dict, Optional, Tuple

from pydantic import Field

from mote.product.config.base import ConfigModel as YamlModel

_PLACEHOLDER_KEYS = frozenset({"", "sk-", "YOUR_API_KEY"})
_HELPER_CACHE: Dict[str, Tuple[float, str]] = {}
_HELPER_TTL_SECONDS = 300.0
_HELPER_TIMEOUT_SECONDS = 30.0


def _now() -> float:
    return time.monotonic()


def _needs_fill(api_key: Any) -> bool:
    if api_key is None:
        return True
    keys = api_key if isinstance(api_key, list) else [api_key]
    if not keys:
        return True
    return all(key is None or key in _PLACEHOLDER_KEYS for key in keys)


def run_api_key_helper(command: str, *, use_cache: bool = True) -> str:
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
    models = merged.get("models")
    if not isinstance(models, dict):
        return None
    helper = models.get("api_key_helper")
    if not helper or not isinstance(helper, str):
        return None
    default = models.get("default")
    current = default.get("api_key") if isinstance(default, dict) else None
    if not _needs_fill(current):
        return None
    key = run_api_key_helper(helper, use_cache=use_cache)
    if not key:
        return None
    if not isinstance(default, dict):
        default = {}
        models["default"] = default
    default["api_key"] = key
    return key


def clear_cache() -> None:
    _HELPER_CACHE.clear()


class SecretsConfig(YamlModel):
    """Storage knobs; core prompt/result protection cannot be disabled."""

    cipher: str = Field(
        default="aes",
        description="Vault key/cipher strategy name. 'aes' = AES-256-GCM with a ~/.mote/vault.key key file.",
    )
    vault_path: Optional[str] = Field(
        default=None,
        description="Override the encrypted vault file location (default ~/.mote/secrets.json).",
    )
    secrets_config_path: Optional[str] = Field(
        default=None,
        description=(
            "Override the plaintext, human-edited named-secret file location "
            "(default ~/.mote/secrets_config.json). A flat {name: value} JSON; "
            "edits/deletes hot-sync into the encrypted vault by mtime."
        ),
    )
