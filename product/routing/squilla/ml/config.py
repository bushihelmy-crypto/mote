#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""ML routing config + model-dir resolution glue (Mote-specific).

The trained model bundle (~75 MB of LightGBM / ONNX / sklearn artifacts) is NOT
vendored into git; it is cached under ``~/.mote/router_models/...``. The small
``router.runtime.yaml`` (thresholds / flag rules / tier mapping) IS vendored next
to this module so post-processing works even when the bundle is absent.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from mote.product.paths import default_runtime_paths

# Phase-3 inference bundle name (matches the rsync'd cache dir).
MODEL_BUNDLE_NAME = "v4.2_phase3_inference"

# Bundled config resource (vendored — small, no weights).
_BUNDLED_RUNTIME_YAML = Path(__file__).parent / "router.runtime.yaml"


def default_model_dir() -> Path:
    """Return the cache directory expected to hold the trained model bundle."""
    return default_runtime_paths().user_config_root / "router_models" / MODEL_BUNDLE_NAME


def load_runtime_config(model_dir: str | Path | None = None) -> dict:
    """Load the routing runtime config (thresholds / flags / tier mapping).

    Prefers the bundle-local ``router.runtime.yaml`` (so a deployed bundle can
    override thresholds), falling back to the vendored copy beside this module.
    """
    if model_dir is not None:
        candidate = Path(model_dir) / "router.runtime.yaml"
        if candidate.is_file():
            return yaml.safe_load(candidate.read_text()) or {}
    return yaml.safe_load(_BUNDLED_RUNTIME_YAML.read_text()) or {}
