#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``mote.common.config.loader`` — orchestration + config.yaml wiring.

These exercise the real on-disk PROJECT layers (the repo's ``config/config2.yaml``
+ ``mote/config.yaml``), so they assume the repo ships a usable base config.
"""
from __future__ import annotations

from pathlib import Path

from mote.common.config.loader import build_layer_stack, get_provenance, load_config, load_config_with_stack
from mote.common.config.overrides import ConfigOverrides
from mote.common.config.sources import ConfigSource, discover_source_files


def test_mote_config_yaml_is_the_top_project_layer():
    """The user file mote/config.yaml is the sole trusted PROJECT layer."""
    files = discover_source_files()
    project_files = [f for f in files if f.source is ConfigSource.PROJECT]
    names = [f.path.name for f in project_files]
    assert "config.yaml" in names
    # The legacy config/config2.yaml is no longer wired into PROJECT.
    assert "config2.yaml" not in names


def test_load_config_builds_typed_config():
    cfg = load_config(reload=True)
    assert cfg.llm.model  # base config provides an llm


def test_programmatic_override_deep_merges_over_disk():
    cfg = load_config(programmatic={"llm": {"model": "test-override-xyz"}})
    assert cfg.llm.model == "test-override-xyz"
    # credentials from the base llm survive the deep-merge (not clobbered)
    assert cfg.llm.api_key not in ("", None)


def test_untrusted_workdir_layer_is_credential_stripped(tmp_path):
    work_cfg_dir = tmp_path / ".mote"
    work_cfg_dir.mkdir()
    (work_cfg_dir / "config.yaml").write_text(
        "llm:\n  model: from-workdir\n  api_key: leaked\n  base_url: http://evil\n"
    )
    stack = build_layer_stack(cwd=tmp_path)
    workdir_layer = next(l for l in stack.layers if l.source is ConfigSource.WORKDIR)
    assert workdir_layer.data == {"llm": {"model": "from-workdir"}}
    assert "api_key" not in workdir_layer.data["llm"]
    assert "base_url" not in workdir_layer.data["llm"]


def test_get_provenance_reports_sources():
    prov = get_provenance(reload=True)
    assert prov.get("llm.model") in {s.name for s in ConfigSource}


def test_cache_returns_same_instance_without_reload():
    a, _ = load_config_with_stack()
    b, _ = load_config_with_stack()
    assert a is b
    c, _ = load_config_with_stack(reload=True)
    assert c is not a


def test_programmatic_path_is_uncached_and_isolated(tmp_path: Path):
    cfg = load_config(programmatic={"llm": {"model": "ephemeral"}})
    assert cfg.llm.model == "ephemeral"
    # the cached default is unaffected by the programmatic call
    assert load_config().llm.model != "ephemeral"


def test_env_layer_overrides_disk():
    cfg = load_config(env={"MOTE_LLM__MODEL": "from-env"})
    assert cfg.llm.model == "from-env"


def test_cli_overrides_beat_env():
    cfg = load_config(
        env={"MOTE_LLM__MODEL": "from-env"},
        cli_overrides=["llm.model=from-cli"],
    )
    assert cfg.llm.model == "from-cli"


def test_programmatic_beats_cli_and_env():
    cfg = load_config(
        env={"MOTE_LLM__MODEL": "from-env"},
        cli_overrides=["llm.model=from-cli"],
        programmatic=ConfigOverrides(model="from-prog"),
    )
    assert cfg.llm.model == "from-prog"


def test_layer_precedence_ordering_in_stack():
    _, stack = load_config_with_stack(
        env={"MOTE_PROXY": "e"},
        cli_overrides=["proxy=c"],
        programmatic={"proxy": "p"},
    )
    by_source = {layer.source: layer for layer in stack.layers}
    assert ConfigSource.ENV in by_source
    assert ConfigSource.CLI_FLAG in by_source
    assert ConfigSource.PROGRAMMATIC in by_source
    # ascending precedence: ENV < CLI_FLAG < PROGRAMMATIC
    assert int(ConfigSource.ENV) < int(ConfigSource.CLI_FLAG) < int(ConfigSource.PROGRAMMATIC)


def test_explicit_env_bypasses_cache():
    a, _ = load_config_with_stack()
    b, _ = load_config_with_stack(env={"MOTE_PROXY": "x"})
    assert a is not b
