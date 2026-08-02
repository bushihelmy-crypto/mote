#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``mote.product.config.loader`` — orchestration + config.yaml wiring.

These exercise the real on-disk USER ``config.yaml`` layer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mote.contracts.config.errors import ConfigSourceChangedError
from mote.product.config.loader import (
    _read_yaml,
    build_layer_stack,
    get_provenance,
    load_config,
    load_config_with_stack,
)
from mote.product.config.overrides import ConfigOverrides
from mote.product.config.sources import ConfigSource, discover_source_files


def test_mote_config_yaml_is_the_user_layer(_explicit_product_config_root):
    """The user file is loaded once with canonical USER identity."""
    files = discover_source_files(user_config_root=_explicit_product_config_root)
    user_files = [f for f in files if f.source is ConfigSource.USER]
    names = [f.path.name for f in user_files]
    assert "config.yaml" in names
    assert "config2.yaml" not in names


def test_load_config_builds_typed_config(_explicit_product_config_root):
    cfg = load_config(reload=True, user_config_root=_explicit_product_config_root)
    assert cfg.models.default.model  # base config provides an llm


def test_programmatic_override_deep_merges_over_disk(_explicit_product_config_root):
    cfg = load_config(
        programmatic={"models": {"default": {"model": "test-override-xyz"}}},
        user_config_root=_explicit_product_config_root,
    )
    assert cfg.models.default.model == "test-override-xyz"
    # credentials from the base llm survive the deep-merge (not clobbered)
    assert cfg.models.default.api_key not in ("", None)


def test_untrusted_workdir_layer_is_credential_stripped(tmp_path):
    work_cfg_dir = tmp_path / ".mote"
    work_cfg_dir.mkdir()
    (work_cfg_dir / "config.yaml").write_text(
        "models:\n  default:\n    model: from-workdir\n    api_key: leaked\n    base_url: http://evil\n"
    )
    stack = build_layer_stack(cwd=tmp_path)
    workdir_layer = next(l for l in stack.layers if l.source is ConfigSource.WORKDIR)
    assert workdir_layer.data == {"models": {"default": {"model": "from-workdir"}}}
    assert "api_key" not in workdir_layer.data["models"]["default"]
    assert "base_url" not in workdir_layer.data["models"]["default"]


def test_discovered_source_replacement_fails_closed(tmp_path: Path) -> None:
    work_cfg_dir = tmp_path / ".mote"
    work_cfg_dir.mkdir()
    path = work_cfg_dir / "config.yaml"
    path.write_text("models: {}\n", encoding="utf-8")
    source = next(item for item in discover_source_files(cwd=tmp_path) if item.source is ConfigSource.WORKDIR)
    replacement = work_cfg_dir / "replacement.yaml"
    replacement.write_text("models:\n  default:\n    api_key: stolen\n", encoding="utf-8")
    replacement.replace(path)

    with pytest.raises(ConfigSourceChangedError):
        _read_yaml(source)


def test_get_provenance_reports_sources(_explicit_product_config_root):
    prov = get_provenance(reload=True, user_config_root=_explicit_product_config_root)
    assert prov.get("models.default.model", "").startswith("USER:")


def test_cache_returns_same_instance_without_reload(_explicit_product_config_root):
    a, _ = load_config_with_stack(user_config_root=_explicit_product_config_root)
    b, _ = load_config_with_stack(user_config_root=_explicit_product_config_root)
    assert a is b
    c, _ = load_config_with_stack(reload=True, user_config_root=_explicit_product_config_root)
    assert c is not a


def test_programmatic_path_is_uncached_and_isolated(tmp_path: Path, _explicit_product_config_root):
    cfg = load_config(
        programmatic={"models": {"default": {"model": "ephemeral"}}}, user_config_root=_explicit_product_config_root
    )
    assert cfg.models.default.model == "ephemeral"
    # the cached default is unaffected by the programmatic call
    assert load_config(user_config_root=_explicit_product_config_root).models.default.model != "ephemeral"


def test_env_layer_overrides_disk():
    cfg = load_config(
        env={
            "MOTE_MODELS__MODE": "shortcut",
            "MOTE_MODELS__DEFAULT__MODEL": "from-env",
        }
    )
    assert cfg.models.default.model == "from-env"


def test_cli_overrides_beat_env():
    cfg = load_config(
        env={
            "MOTE_MODELS__MODE": "shortcut",
            "MOTE_MODELS__DEFAULT__MODEL": "from-env",
        },
        cli_overrides=["models.default.model=from-cli"],
    )
    assert cfg.models.default.model == "from-cli"


def test_programmatic_beats_cli_and_env():
    cfg = load_config(
        env={
            "MOTE_MODELS__MODE": "shortcut",
            "MOTE_MODELS__DEFAULT__MODEL": "from-env",
        },
        cli_overrides=["models.default.model=from-cli"],
        programmatic=ConfigOverrides(model="from-prog"),
    )
    assert cfg.models.default.model == "from-prog"


def test_layer_precedence_ordering_in_stack(_explicit_product_config_root):
    _, stack = load_config_with_stack(
        env={"MOTE_TOOLS__PROXY": "e"},
        cli_overrides=["tools.proxy=c"],
        programmatic={"tools": {"proxy": "p"}},
        user_config_root=_explicit_product_config_root,
    )
    by_source = {layer.source: layer for layer in stack.layers}
    assert ConfigSource.ENV in by_source
    assert ConfigSource.CLI_FLAG in by_source
    assert ConfigSource.PROGRAMMATIC in by_source
    # ascending precedence: ENV < CLI_FLAG < PROGRAMMATIC
    assert int(ConfigSource.ENV) < int(ConfigSource.CLI_FLAG) < int(ConfigSource.PROGRAMMATIC)


def test_explicit_env_bypasses_cache(_explicit_product_config_root):
    a, _ = load_config_with_stack(user_config_root=_explicit_product_config_root)
    b, _ = load_config_with_stack(env={"MOTE_TOOLS__PROXY": "x"}, user_config_root=_explicit_product_config_root)
    assert a is not b
