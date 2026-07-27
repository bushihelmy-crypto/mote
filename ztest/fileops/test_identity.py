from __future__ import annotations

import os

import pytest

from mote.runtime.fileops.identity import name_identity, path_token, project_identity, target_identity


def test_hardlink_aliases_share_target_but_not_name_identity(tmp_path):
    target = tmp_path / "target.txt"
    alias = tmp_path / "alias.txt"
    target.write_bytes(b"same inode")
    os.link(target, alias)

    assert target_identity(target) == target_identity(alias)
    assert name_identity(target) != name_identity(alias)


def test_symlink_alias_resolves_to_same_target_identity(tmp_path):
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable")
    target = tmp_path / "target.txt"
    alias = tmp_path / "alias.txt"
    target.write_bytes(b"same target")
    alias.symlink_to(target)

    assert target_identity(target) == target_identity(alias)


def test_missing_name_identity_is_stable(tmp_path):
    first = name_identity(tmp_path / "future.txt")
    second = name_identity(tmp_path / "." / "future.txt")

    assert first == second


def test_project_identity_uses_directory_identity_not_spelling(tmp_path):
    assert project_identity(tmp_path) == project_identity(tmp_path / ".")


@pytest.mark.skipif(os.name != "posix", reason="bytes paths are POSIX-only")
def test_non_utf8_path_token_roundtrips_native_bytes(tmp_path):
    parent = os.fsencode(tmp_path)
    native = os.path.join(parent, b"bad-\xff-name")

    token = path_token(native)

    assert token.native == native
    assert os.fsencode(token.display) == native
