#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the MANAGED admin-policy layer (highest precedence, locks below)."""
from __future__ import annotations

import mote.common.config.sources as sources_mod
from mote.common.config.layers import ConfigLayer, ConfigLayerStack
from mote.common.config.sources import ConfigSource, discover_source_files


def test_managed_is_highest_precedence():
    assert int(ConfigSource.MANAGED) > int(ConfigSource.PROGRAMMATIC)


def test_managed_is_trusted():
    assert ConfigSource.MANAGED.trusted is True


def test_managed_file_discovered_when_present(tmp_path, monkeypatch):
    monkeypatch.setattr(sources_mod, "_SYSTEM_CONFIG_DIR", tmp_path)
    (tmp_path / "managed.config.yaml").write_text("proxy: policy\n")
    files = discover_source_files()
    managed = [f for f in files if f.source is ConfigSource.MANAGED]
    assert len(managed) == 1
    assert managed[0].path.name == "managed.config.yaml"


def test_no_managed_file_means_no_managed_layer(tmp_path, monkeypatch):
    monkeypatch.setattr(sources_mod, "_SYSTEM_CONFIG_DIR", tmp_path)
    files = discover_source_files()
    assert not any(f.source is ConfigSource.MANAGED for f in files)


def test_managed_overrides_programmatic_in_merge():
    # Even the highest runtime layer (programmatic) loses to managed policy.
    stack = ConfigLayerStack()
    stack.add(ConfigLayer(source=ConfigSource.PROGRAMMATIC, data={"proxy": "from-code"}))
    stack.add(ConfigLayer(source=ConfigSource.MANAGED, data={"proxy": "from-admin"}))
    assert stack.effective()["proxy"] == "from-admin"
    assert stack.provenance()["proxy"] == "MANAGED"
