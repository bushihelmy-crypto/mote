#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for Product ``.mote`` project-path discovery.

These pin the upward walk: from *cwd* up to (and including)
the git root, collect every existing ``<dir>/.mote/<subdir>`` (or file), returned
**low→high precedence** (git root first, cwd last) so a closer directory can
override a farther one. The walk stops at the git-root boundary so assets from a
parent directory outside the repo never leak in.

The git root is faked by monkeypatching ``find_git_root`` on the path module's
call-time import target, so nothing depends on the test tree's real VCS layout.
"""
from mote.product.paths import (
    MOTE_DIR_NAME,
    discovery,
    mote_project_dirs,
    mote_project_files,
    mote_source_dirs,
    user_mote_dir,
)


def _fake_git_root(monkeypatch, root):
    """Make ``find_git_root`` report *root* for every query (deferred-import safe)."""
    monkeypatch.setattr(discovery, "find_git_root", lambda cwd: str(root))


class TestUserMoteDir:
    def test_anchored_at_explicit_config_root(self, tmp_path):
        assert user_mote_dir("skills", user_config_root=tmp_path) == tmp_path / "skills"

    def test_dir_name_constant(self):
        assert MOTE_DIR_NAME == ".mote"


class TestProjectDirs:
    def test_collects_existing_up_to_git_root(self, tmp_path, monkeypatch):
        # tree: root/.mote/skills and root/sub/.mote/skills both exist.
        root = tmp_path
        sub = root / "sub"
        (root / MOTE_DIR_NAME / "skills").mkdir(parents=True)
        (sub / MOTE_DIR_NAME / "skills").mkdir(parents=True)
        _fake_git_root(monkeypatch, root)

        dirs = mote_project_dirs("skills", sub)
        # low→high: git root first, cwd last.
        assert dirs == [root / MOTE_DIR_NAME / "skills", sub / MOTE_DIR_NAME / "skills"]

    def test_skips_missing_dirs(self, tmp_path, monkeypatch):
        root = tmp_path
        sub = root / "sub"
        sub.mkdir()
        (root / MOTE_DIR_NAME / "skills").mkdir(parents=True)  # only root has it
        _fake_git_root(monkeypatch, root)

        assert mote_project_dirs("skills", sub) == [root / MOTE_DIR_NAME / "skills"]

    def test_stops_at_git_root_boundary(self, tmp_path, monkeypatch):
        # A .mote above the git root must NOT be collected.
        outer = tmp_path
        root = outer / "repo"
        root.mkdir()
        (outer / MOTE_DIR_NAME / "skills").mkdir(parents=True)  # above the boundary
        (root / MOTE_DIR_NAME / "skills").mkdir(parents=True)
        _fake_git_root(monkeypatch, root)

        assert mote_project_dirs("skills", root) == [root / MOTE_DIR_NAME / "skills"]

    def test_empty_when_none_exist(self, tmp_path, monkeypatch):
        _fake_git_root(monkeypatch, tmp_path)
        assert mote_project_dirs("skills", tmp_path) == []


class TestProjectFiles:
    def test_collects_existing_files_up_to_root(self, tmp_path, monkeypatch):
        root = tmp_path
        sub = root / "sub"
        (root / MOTE_DIR_NAME).mkdir(parents=True)
        (sub / MOTE_DIR_NAME).mkdir(parents=True)
        far = root / MOTE_DIR_NAME / "mcp.json"
        near = sub / MOTE_DIR_NAME / "mcp.json"
        far.write_text("{}", encoding="utf-8")
        near.write_text("{}", encoding="utf-8")
        _fake_git_root(monkeypatch, root)

        assert mote_project_files("mcp.json", sub) == [far, near]

    def test_empty_when_no_files(self, tmp_path, monkeypatch):
        _fake_git_root(monkeypatch, tmp_path)
        assert mote_project_files("mcp.json", tmp_path) == []


class TestSourceDirs:
    def test_user_dir_leads_then_project_walk(self, tmp_path, monkeypatch):
        root = tmp_path
        (root / MOTE_DIR_NAME / "agents").mkdir(parents=True)
        _fake_git_root(monkeypatch, root)

        dirs = mote_source_dirs("agents", root)
        # user dir is always first (lowest layer), even if it doesn't exist.
        assert dirs[0] == user_mote_dir("agents")
        assert dirs[-1] == root / MOTE_DIR_NAME / "agents"
