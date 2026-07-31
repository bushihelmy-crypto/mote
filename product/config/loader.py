#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Config loader: discover layers, deep-merge, build the typed Config.

This is the orchestration layer of the config-center pipeline::

    files + env + cli + programmatic
          -> ConfigLayerStack (raw + provenance)
          -> deep-merge -> merged dict
          -> Config (typed pydantic root)

Precedence (low -> high): defaults < system < user < project < workdir <
profile < env < cli-flags < programmatic < managed. Untrusted (WORKDIR) layers
are credential-stripped on the way in; the MANAGED admin-policy layer overrides
everything (including programmatic code). The default result is cached per
``(cwd, profile)``; any call-specific input (explicit ``env``, ``cli_overrides``
or ``programmatic``) bypasses the cache.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple, Union

import yaml

from mote.contracts.config.errors import UnknownConfigKeysError
from mote.product.config.diagnostics import unknown_key_paths
from mote.product.config.env import build_env_layer
from mote.product.config.layers import ConfigLayer, ConfigLayerStack, strip_sensitive
from mote.product.config.overrides import ConfigOverrides, parse_cli_overrides
from mote.product.config.schema import Config
from mote.product.config.secrets import resolve_api_key
from mote.product.config.sources import ConfigSource, discover_source_files

Programmatic = Union[Dict[str, Any], ConfigOverrides]

# Env var that selects the active profile when none is passed explicitly.
PROFILE_ENV_VAR = "MOTE_PROFILE"


def _read_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as stream:
        return yaml.safe_load(stream) or {}


def _resolve_profile(profile: Optional[str], env: Optional[Mapping[str, str]]) -> Optional[str]:
    """Pick the active profile: explicit arg wins, else ``MOTE_PROFILE``.

    The env source mirrors the layer-env selection: an explicit ``env`` mapping
    is consulted when given, otherwise the process environment.
    """
    if profile:
        return profile
    source = env if env is not None else os.environ
    return source.get(PROFILE_ENV_VAR) or None


def _programmatic_dict(
    programmatic: Optional[Programmatic],
) -> Optional[Dict[str, Any]]:
    """Normalize a programmatic override (dict or ConfigOverrides) to a dict."""
    if programmatic is None:
        return None
    if isinstance(programmatic, ConfigOverrides):
        data = programmatic.to_layer_dict()
        return data or None
    return programmatic or None


def build_layer_stack(
    cwd: Optional[Path] = None,
    *,
    profile: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    cli_overrides: Optional[Iterable[str]] = None,
    programmatic: Optional[Programmatic] = None,
    user_config_root: Path | None = None,
    source_root: Path | None = None,
) -> ConfigLayerStack:
    """Assemble the full layer stack: disk files + env + cli flags + programmatic.

    With ``env=None`` the process environment is read, so the ENV layer applies
    automatically on the default load path. ``profile`` (or ``MOTE_PROFILE``)
    adds the named overlay as a trusted PROFILE layer.
    """
    profile = _resolve_profile(profile, env)
    stack = ConfigLayerStack()
    for source_file in discover_source_files(
        cwd,
        profile=profile,
        user_config_root=user_config_root,
        source_root=source_root,
    ):
        data = _read_yaml(source_file.path)
        if not source_file.source.trusted:
            data = strip_sensitive(data)
        stack.add(ConfigLayer(source=source_file.source, data=data, path=source_file.path))

    env_data = build_env_layer(env)
    if env_data:
        stack.add(ConfigLayer(source=ConfigSource.ENV, data=env_data))

    cli_data = parse_cli_overrides(cli_overrides)
    if cli_data:
        stack.add(ConfigLayer(source=ConfigSource.CLI_FLAG, data=cli_data))

    prog = _programmatic_dict(programmatic)
    if prog:
        stack.add(ConfigLayer(source=ConfigSource.PROGRAMMATIC, data=dict(prog)))

    return stack


# (cwd, profile) -> (Config, ConfigLayerStack); only for the plain default load.
_CACHE: Dict[str, Tuple["Config", ConfigLayerStack]] = {}


def _cache_key(
    cwd: Optional[Path],
    profile: Optional[str],
    user_config_root: Path | None,
    source_root: Path | None,
) -> str:
    base = str(Path(cwd) if cwd is not None else Path.cwd())
    return f"{base}\0{profile or ''}\0{user_config_root or ''}\0{source_root or ''}"


def _build_config(stack: ConfigLayerStack) -> "Config":
    """Construct the typed Config after rejecting every unknown key."""
    merged = stack.effective()
    # Fill llm.api_key from api_key_helper when no static/env key is present.
    resolve_api_key(merged)
    unknown = unknown_key_paths(merged, Config)
    if unknown:
        raise UnknownConfigKeysError(unknown)
    return Config(**merged)


def load_config_with_stack(
    cwd: Optional[Path] = None,
    *,
    reload: bool = False,
    profile: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    cli_overrides: Optional[Iterable[str]] = None,
    programmatic: Optional[Programmatic] = None,
    user_config_root: Path | None = None,
    source_root: Path | None = None,
) -> Tuple["Config", ConfigLayerStack]:
    """Build the merged :class:`Config` and return it with its layer stack."""
    use_cache = env is None and not cli_overrides and not programmatic
    if use_cache:
        resolved_profile = _resolve_profile(profile, env)
        key = _cache_key(cwd, resolved_profile, user_config_root, source_root)
        if reload or key not in _CACHE:
            stack = build_layer_stack(
                cwd,
                profile=profile,
                user_config_root=user_config_root,
                source_root=source_root,
            )
            _CACHE[key] = (_build_config(stack), stack)
        return _CACHE[key]

    stack = build_layer_stack(
        cwd,
        profile=profile,
        env=env,
        cli_overrides=cli_overrides,
        programmatic=programmatic,
        user_config_root=user_config_root,
        source_root=source_root,
    )
    return _build_config(stack), stack


def load_config(
    cwd: Optional[Path] = None,
    *,
    reload: bool = False,
    profile: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    cli_overrides: Optional[Iterable[str]] = None,
    programmatic: Optional[Programmatic] = None,
    user_config_root: Path | None = None,
    source_root: Path | None = None,
) -> "Config":
    """Load the merged, typed :class:`Config` (the common entry point)."""
    return load_config_with_stack(
        cwd,
        reload=reload,
        profile=profile,
        env=env,
        cli_overrides=cli_overrides,
        programmatic=programmatic,
        user_config_root=user_config_root,
        source_root=source_root,
    )[0]


def get_provenance(
    cwd: Optional[Path] = None,
    *,
    reload: bool = False,
    profile: Optional[str] = None,
    user_config_root: Path | None = None,
    source_root: Path | None = None,
) -> Dict[str, str]:
    """Return the dotted-path -> source map for diagnostics ("where from?")."""
    return load_config_with_stack(
        cwd,
        reload=reload,
        profile=profile,
        user_config_root=user_config_root,
        source_root=source_root,
    )[1].provenance()
