#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for ``metagpt.executor.permission.sandbox.guard.SandboxGuard``.

Covers the three modes (full / read-only / workspace-write), cwd + configured
writable-root containment, empty-path passthrough, symlink-resolved containment,
and interactively-granted session roots.
"""
from __future__ import annotations

import os

from metagpt.common.schema import SandboxConfig
from metagpt.executor.permission.sandbox import SandboxGuard


class TestFullMode:
    def test_full_allows_any_path(self):
        guard = SandboxGuard(SandboxConfig(mode="full"))
        assert guard.check_write("/etc/passwd").allowed
        assert guard.check_write("/anywhere/else.txt").allowed


class TestReadOnlyMode:
    def test_read_only_blocks_writes(self):
        guard = SandboxGuard(SandboxConfig(mode="read-only"))
        verdict = guard.check_write("/tmp/x.txt")
        assert not verdict.allowed
        assert "read-only" in verdict.reason

    def test_read_only_empty_path_allowed(self):
        # No concrete path to gate — passthrough even in read-only.
        guard = SandboxGuard(SandboxConfig(mode="read-only"))
        assert guard.check_write("").allowed


class TestWorkspaceWrite:
    def test_inside_cwd_allowed(self, tmp_path):
        cwd = str(tmp_path)
        guard = SandboxGuard(SandboxConfig(mode="workspace-write"), get_cwd=lambda: cwd)
        assert guard.check_write(os.path.join(cwd, "sub", "f.txt")).allowed

    def test_cwd_root_itself_allowed(self, tmp_path):
        cwd = str(tmp_path)
        guard = SandboxGuard(SandboxConfig(mode="workspace-write"), get_cwd=lambda: cwd)
        assert guard.check_write(cwd).allowed

    def test_outside_cwd_blocked(self, tmp_path):
        cwd = str(tmp_path / "workspace")
        os.makedirs(cwd, exist_ok=True)
        guard = SandboxGuard(SandboxConfig(mode="workspace-write"), get_cwd=lambda: cwd)
        verdict = guard.check_write(str(tmp_path / "outside.txt"))
        assert not verdict.allowed
        assert "outside" in verdict.reason

    def test_configured_writable_root_allowed(self, tmp_path):
        extra = tmp_path / "extra"
        extra.mkdir()
        guard = SandboxGuard(
            SandboxConfig(mode="workspace-write", writable_roots=[str(extra)]),
            get_cwd=lambda: str(tmp_path / "ws"),
        )
        assert guard.check_write(str(extra / "f.txt")).allowed

    def test_sibling_prefix_not_treated_as_inside(self, tmp_path):
        # '/ws' must not match '/wsX' — containment is path-segment aware.
        ws = tmp_path / "ws"
        ws.mkdir()
        sibling = tmp_path / "wsX"
        sibling.mkdir()
        guard = SandboxGuard(SandboxConfig(mode="workspace-write"), get_cwd=lambda: str(ws))
        assert not guard.check_write(str(sibling / "f.txt")).allowed

    def test_empty_path_allowed(self, tmp_path):
        guard = SandboxGuard(SandboxConfig(mode="workspace-write"), get_cwd=lambda: str(tmp_path))
        assert guard.check_write("").allowed


class TestSessionRoots:
    def test_added_session_root_allows_writes(self, tmp_path):
        cwd = tmp_path / "ws"
        cwd.mkdir()
        granted = tmp_path / "granted"
        granted.mkdir()
        guard = SandboxGuard(SandboxConfig(mode="workspace-write"), get_cwd=lambda: str(cwd))
        assert not guard.check_write(str(granted / "f.txt")).allowed
        guard.add_session_root(str(granted))
        assert guard.check_write(str(granted / "f.txt")).allowed

    def test_writable_roots_includes_cwd_and_config_and_session(self, tmp_path):
        cwd = tmp_path / "ws"
        cwd.mkdir()
        cfg_root = tmp_path / "cfg"
        cfg_root.mkdir()
        sess_root = tmp_path / "sess"
        sess_root.mkdir()
        guard = SandboxGuard(
            SandboxConfig(mode="workspace-write", writable_roots=[str(cfg_root)]),
            get_cwd=lambda: str(cwd),
        )
        guard.add_session_root(str(sess_root))
        roots = guard.writable_roots()
        assert os.path.realpath(str(cwd)) in roots
        assert os.path.realpath(str(cfg_root)) in roots
        assert os.path.realpath(str(sess_root)) in roots
