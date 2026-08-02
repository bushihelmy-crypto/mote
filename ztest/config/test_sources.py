#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``mote.product.config.sources`` — precedence + file discovery."""

from __future__ import annotations

from mote.product.config.sources import CONFIG_FILE_NAME, ConfigSource, discover_source_files


def test_precedence_is_strictly_ascending():
    order = [
        ConfigSource.DEFAULT,
        ConfigSource.SYSTEM,
        ConfigSource.USER,
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
    for s in (
        ConfigSource.SYSTEM,
        ConfigSource.USER,
        ConfigSource.PROGRAMMATIC,
    ):
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
    assert workdir_files[0].path == (work_cfg_dir / CONFIG_FILE_NAME).resolve()


def test_discover_skips_missing_workdir(tmp_path):
    files = discover_source_files(cwd=tmp_path)
    assert all(f.source is not ConfigSource.WORKDIR for f in files)


def test_same_inode_user_and_workdir_alias_is_loaded_once_as_untrusted(tmp_path):
    user_root = tmp_path / ".mote"
    user_root.mkdir()
    path = user_root / CONFIG_FILE_NAME
    path.write_text("tools:\n  proxy: local\n")
    path.chmod(0o600)

    files = discover_source_files(cwd=tmp_path, user_config_root=user_root)
    matches = [item for item in files if item.identity.inode == path.stat().st_ino]

    assert len(matches) == 1
    assert matches[0].source is ConfigSource.WORKDIR
    assert matches[0].trusted is False


def test_user_symlink_outside_canonical_root_is_untrusted(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    target = checkout / CONFIG_FILE_NAME
    target.write_text("tools:\n  proxy: checkout\n")
    target.chmod(0o600)
    user_root = tmp_path / "user"
    user_root.mkdir()
    (user_root / CONFIG_FILE_NAME).symlink_to(target)

    [source] = discover_source_files(cwd=tmp_path / "empty", user_config_root=user_root)

    assert source.path == target.resolve()
    assert source.trusted is False


def test_user_file_requires_owner_safe_permissions(tmp_path):
    user_root = tmp_path / "user"
    user_root.mkdir()
    path = user_root / CONFIG_FILE_NAME
    path.write_text("tools:\n  proxy: user\n")
    path.chmod(0o666)

    [source] = discover_source_files(cwd=tmp_path, user_config_root=user_root)
    assert source.trusted is False

    path.chmod(0o600)
    [source] = discover_source_files(cwd=tmp_path, user_config_root=user_root)
    assert source.trusted is True
