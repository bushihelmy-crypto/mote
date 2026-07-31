#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Config layer stack, deep-merge and provenance.

The merge engine is deliberately decoupled from the typed :class:`Config`
schema (codex's ``ConfigLayerStack`` -> ``ConfigToml`` -> ``Config`` split):
this module only deals with raw nested dicts.

Merge semantics (best-of-both):
- dict: recursive deep-merge (last writer wins per key).
- list: union + dedupe, low-layer items first — so permission
  allow/deny rules, mcp servers, additional dirs accumulate across layers.
- scalar / type mismatch: higher layer wins.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from mote.product.config.merge_primitives import deep_merge
from mote.product.config.model.merge import ModelLayer, merge_product_model_layers
from mote.product.config.sources import ConfigSource

# Credential / endpoint-redirecting keys removed from untrusted layers, at any
# nesting depth (a malicious working-dir config must not steer LLM auth, nor
# inject ``api_key_helper`` — an arbitrary shell command run to fetch a key).
CREDENTIAL_DENYLIST = frozenset({"api_key", "base_url", "oauth", "model_providers", "api_key_helper"})


def strip_sensitive(data: Any) -> Any:
    """Recursively drop :data:`CREDENTIAL_DENYLIST` keys from a nested dict."""
    if isinstance(data, dict):
        return {k: strip_sensitive(v) for k, v in data.items() if k not in CREDENTIAL_DENYLIST}
    if isinstance(data, list):
        return [strip_sensitive(v) for v in data]
    return data


def _record_origin(data: Any, source_name: str, origin: Dict[str, str], prefix: str = "") -> None:
    """Walk ``data`` recording dotted-path -> source for scalar/list leaves.

    A nested dict is recursed into; any non-dict leaf (scalar or list) is
    recorded at its dotted path, last writer wins (layers visited low->high).
    """
    if isinstance(data, dict):
        for key, value in data.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                _record_origin(value, source_name, origin, path)
            else:
                origin[path] = source_name


@dataclass
class ConfigLayer:
    """One raw config layer: its source, parsed data, and originating file."""

    source: ConfigSource
    data: Dict[str, Any]
    path: Optional[Path] = None


@dataclass
class ConfigLayerStack:
    """An ordered collection of :class:`ConfigLayer` (stored low->high)."""

    layers: List[ConfigLayer] = field(default_factory=list)

    def add(self, layer: ConfigLayer) -> "ConfigLayerStack":
        self.layers.append(layer)
        return self

    def sorted_layers(self) -> List[ConfigLayer]:
        """Layers in ascending precedence (stable on insertion order ties)."""
        return sorted(self.layers, key=lambda layer: int(layer.source))

    def effective(self) -> Dict[str, Any]:
        """Left-fold deep-merge all layers; higher precedence has the last word."""
        merged: Dict[str, Any] = {}
        model_layers: list[ModelLayer] = []
        for layer in self.sorted_layers():
            ordinary = {key: value for key, value in layer.data.items() if key != "models"}
            merged = deep_merge(merged, ordinary)
            if isinstance(layer.data.get("models"), dict):
                model_layers.append(
                    ModelLayer(
                        layer.source.name,
                        layer.data["models"],
                        trusted=layer.source.trusted,
                        display_source=(
                            f"{layer.source.name}:{layer.path.name}" if layer.path is not None else layer.source.name
                        ),
                    )
                )
        if model_layers:
            model_result = merge_product_model_layers(model_layers)
            merged["models"] = model_result.data
        return merged

    def provenance(self) -> Dict[str, str]:
        """Dotted-path -> source name, answering "where did this value come from?"."""
        origin: Dict[str, str] = {}
        model_layers: list[ModelLayer] = []
        for layer in self.sorted_layers():
            ordinary = {key: value for key, value in layer.data.items() if key != "models"}
            _record_origin(ordinary, layer.source.name, origin)
            if isinstance(layer.data.get("models"), dict):
                model_layers.append(
                    ModelLayer(
                        layer.source.name,
                        layer.data["models"],
                        trusted=layer.source.trusted,
                        display_source=(
                            f"{layer.source.name}:{layer.path.name}" if layer.path is not None else layer.source.name
                        ),
                    )
                )
        if model_layers:
            origin.update(merge_product_model_layers(model_layers).provenance)
        return origin
