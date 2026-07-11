#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Runtime override surfaces: ``-c key=value`` CLI flags and typed overrides.

Two highest-precedence input channels for the config center:
- :func:`parse_cli_overrides` turns ``-c a.b=c`` strings into a nested dict
  (the CLI_FLAG layer; mirrors codex's SessionFlags).
- :class:`ConfigOverrides` is a typed, validated programmatic override bag
  (the PROGRAMMATIC layer, the very top of the precedence stack).
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field

from mote.common.config.layers import deep_merge


def parse_override_value(raw: str) -> Any:
    """Parse a raw string into a typed value via YAML, falling back to the string.

    So ``enable_router=true`` -> ``True``, ``llm.max_token=8000`` -> ``8000``,
    ``llm.model=claude-opus`` -> ``"claude-opus"``. An empty string stays ``""``.
    """
    if raw == "":
        return ""
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError:
        return raw


def set_nested(data: Dict[str, Any], segments: List[str], value: Any) -> None:
    """Insert ``value`` at the nested ``segments`` path, creating dicts as needed.

    A non-dict value sitting in an intermediate slot is replaced with a dict
    (last-write-wins) so a deeper key can't crash on a scalar.
    """
    cursor = data
    for seg in segments[:-1]:
        nxt = cursor.get(seg)
        if not isinstance(nxt, dict):
            nxt = {}
            cursor[seg] = nxt
        cursor = nxt
    cursor[segments[-1]] = value


def parse_cli_overrides(items: Optional[Iterable[str]]) -> Dict[str, Any]:
    """Turn ``-c key.path=value`` strings into one nested override dict.

    Each item is split on the first ``=`` (values may contain ``=``); the LHS is
    a dot-separated path. Raises ``ValueError`` on a malformed item.
    """
    data: Dict[str, Any] = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"Invalid -c override (expected key=value): {item!r}")
        key, _, raw = item.partition("=")
        segments = [s for s in key.strip().split(".") if s]
        if not segments:
            raise ValueError(f"Invalid -c override (empty key): {item!r}")
        set_nested(data, segments, parse_override_value(raw))
    return data


class ConfigOverrides(BaseModel):
    """Typed, highest-precedence programmatic overrides (the PROGRAMMATIC layer).

    Common knobs are first-class and validated; ``extra`` is a free-form nested
    escape hatch that deep-merges last, so it can reach any config key.
    """

    model_config = ConfigDict(protected_namespaces=())

    model: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    proxy: Optional[str] = None
    enable_router: Optional[bool] = None
    extra: Dict[str, Any] = Field(default_factory=dict)

    def to_layer_dict(self) -> Dict[str, Any]:
        """Render the overrides as a nested dict ready to merge as a layer."""
        data: Dict[str, Any] = {}
        llm: Dict[str, Any] = {}
        if self.model is not None:
            llm["model"] = self.model
        if self.api_key is not None:
            llm["api_key"] = self.api_key
        if self.base_url is not None:
            llm["base_url"] = self.base_url
        if llm:
            data["llm"] = llm
        if self.proxy is not None:
            data["proxy"] = self.proxy
        if self.enable_router is not None:
            data["enable_router"] = self.enable_router
        if self.extra:
            data = deep_merge(data, self.extra)
        return data
