#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the PROFILE overlay layer (``~/.agentframe/<name>.config.yaml``)."""
from __future__ import annotations

import metagpt.common.config.sources as sources_mod
from metagpt.common.config.loader import build_layer_stack
from metagpt.common.config.sources import ConfigSource, discover_source_files


def _point_user_dir_at(tmp_path, monkeypatch):
    """Redirect the user config dir so profile files resolve under tmp_path."""
    monkeypatch.setattr(sources_mod, "_USER_CONFIG_DIR", tmp_path)


def test_no_profile_means_no_profile_layer():
    files = discover_source_files()
    assert not any(f.source is ConfigSource.PROFILE for f in files)


def test_profile_file_discovered_when_named(tmp_path, monkeypatch):
    _point_user_dir_at(tmp_path, monkeypatch)
    (tmp_path / "work.config.yaml").write_text("proxy: http://profile\n")
    files = discover_source_files(profile="work")
    profiles = [f for f in files if f.source is ConfigSource.PROFILE]
    assert len(profiles) == 1
    assert profiles[0].path.name == "work.config.yaml"


def test_missing_profile_file_is_silently_skipped(tmp_path, monkeypatch):
    _point_user_dir_at(tmp_path, monkeypatch)
    files = discover_source_files(profile="does-not-exist")
    assert not any(f.source is ConfigSource.PROFILE for f in files)


def test_profile_overlay_overrides_lower_layers(tmp_path, monkeypatch):
    _point_user_dir_at(tmp_path, monkeypatch)
    (tmp_path / "work.config.yaml").write_text("proxy: from-profile\n")
    stack = build_layer_stack(profile="work")
    assert stack.effective().get("proxy") == "from-profile"


def test_profile_is_below_env_and_cli(tmp_path, monkeypatch):
    _point_user_dir_at(tmp_path, monkeypatch)
    (tmp_path / "work.config.yaml").write_text("proxy: from-profile\n")
    stack = build_layer_stack(
        profile="work",
        env={"AGENTFRAME_PROXY": "from-env"},
        cli_overrides=["proxy=from-cli"],
    )
    # ENV (50) and CLI_FLAG (60) both outrank PROFILE (40)
    assert stack.effective().get("proxy") == "from-cli"
    assert int(ConfigSource.WORKDIR) < int(ConfigSource.PROFILE) < int(ConfigSource.ENV)


def test_profile_selected_via_env_var(tmp_path, monkeypatch):
    _point_user_dir_at(tmp_path, monkeypatch)
    (tmp_path / "work.config.yaml").write_text("proxy: env-selected-profile\n")
    # No explicit profile arg; AGENTFRAME_PROFILE picks it.
    stack = build_layer_stack(env={"AGENTFRAME_PROFILE": "work"})
    assert stack.effective().get("proxy") == "env-selected-profile"


def test_profile_layer_is_trusted_credentials_survive(tmp_path, monkeypatch):
    _point_user_dir_at(tmp_path, monkeypatch)
    (tmp_path / "work.config.yaml").write_text("llm:\n  api_key: profile-key\n  base_url: http://profile\n")
    stack = build_layer_stack(profile="work")
    layer = next(l for l in stack.layers if l.source is ConfigSource.PROFILE)
    # PROFILE is trusted: unlike WORKDIR, its credentials are NOT stripped.
    assert layer.data["llm"]["api_key"] == "profile-key"
    assert layer.data["llm"]["base_url"] == "http://profile"
