#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``mote.common.config.sources`` — precedence + file discovery."""
from __future__ import annotations

from mote.common.config.sources import CONFIG_FILE_NAME, ConfigSource, discover_source_files


def test_precedence_is_strictly_ascending():
    order = [
        ConfigSource.DEFAULT,
        ConfigSource.SYSTEM,
        ConfigSource.USER,
        ConfigSource.PROJECT,
        ConfigSource.WORKDIR,
        ConfigSource.PROFILE,
        ConfigSource.ENV,
        ConfigSource.CLI_FLAG,
        ConfigSource.PROGRAMMATIC,
    ]
    values = [int(s) for s in order]
    assert values == sorted(values)
    assert len(set(values)) == len(values)


def test_only_workdir_is_untrusted():
    assert ConfigSource.WORKDIR.trusted is False
    for s in (ConfigSource.SYSTEM, ConfigSource.USER, ConfigSource.PROJECT, ConfigSource.PROGRAMMATIC):
        assert s.trusted is True


def test_discover_returns_files_in_ascending_precedence(tmp_path):
    work_cfg_dir = tmp_path / ".mote"
    work_cfg_dir.mkdir()
    (work_cfg_dir / CONFIG_FILE_NAME).write_text("proxy: tmp\n")

    files = discover_source_files(cwd=tmp_path)
    sources = [f.source for f in files]
    assert sources == sorted(sources, key=int)

    # the workdir file is discovered as the (highest) WORKDIR layer
    workdir_files = [f for f in files if f.source is ConfigSource.WORKDIR]
    assert len(workdir_files) == 1
    assert workdir_files[0].path == work_cfg_dir / CONFIG_FILE_NAME


def test_discover_skips_missing_workdir(tmp_path):
    files = discover_source_files(cwd=tmp_path)
    assert all(f.source is not ConfigSource.WORKDIR for f in files)
