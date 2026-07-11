#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the sandbox adapter (``executor.permission.sandbox.adapter``).

Verifies the translation from our ``SandboxGuard`` + ``SandboxRuntimeConfig``
into a runtime ``SandboxPolicy`` / ``SandboxRuntime``: writable-root passthrough,
forced-read-only metadata overrides, and a wired runtime whose policy provider
reflects live guard state.
"""
from __future__ import annotations

import os

from mote.common.schema import SandboxConfig, SandboxRuntimeConfig
from mote.executor.permission.sandbox.adapter import build_policy, build_runtime
from mote.executor.permission.sandbox.guard import SandboxGuard


def _guard(cwd: str) -> SandboxGuard:
    cfg = SandboxConfig(mode="workspace-write")
    return SandboxGuard(cfg, get_cwd=lambda: cwd)


class TestBuildPolicy:
    def test_writable_roots_from_guard(self, tmp_path):
        guard = _guard(str(tmp_path))
        policy = build_policy(guard, cwd=str(tmp_path))
        real = os.path.realpath(str(tmp_path))
        assert real in [os.path.realpath(r) for r in policy.writable_roots]

    def test_metadata_paths_forced_readonly(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / "config.yaml").write_text("k: v")
        guard = _guard(str(tmp_path))
        policy = build_policy(guard, cwd=str(tmp_path))
        overrides = [os.path.realpath(p) for p in policy.readonly_overrides]
        assert os.path.realpath(str(tmp_path / ".git")) in overrides
        assert os.path.realpath(str(tmp_path / "config.yaml")) in overrides

    def test_nonexistent_metadata_skipped(self, tmp_path):
        guard = _guard(str(tmp_path))
        policy = build_policy(guard, cwd=str(tmp_path))
        # No .git / config.yaml created -> no overrides.
        assert policy.readonly_overrides == []

    def test_unshare_net_false_p1(self, tmp_path):
        policy = build_policy(_guard(str(tmp_path)), cwd=str(tmp_path))
        assert policy.unshare_net is False


class TestBuildRuntime:
    def test_runtime_config_passthrough(self, tmp_path):
        cfg = SandboxRuntimeConfig(enabled=True, backend="none", network="open", harden_process=True)
        rt = build_runtime(
            cfg,
            get_cwd=lambda: str(tmp_path),
            guard_factory=lambda: _guard(str(tmp_path)),
        )
        assert rt is not None
        # The provider reflects the live guard's writable roots + cwd.
        policy = rt._policy_for(str(tmp_path))
        real = os.path.realpath(str(tmp_path))
        assert real in [os.path.realpath(r) for r in policy.writable_roots]

    def test_session_grant_visible_next_call(self, tmp_path):
        guard = _guard(str(tmp_path))
        cfg = SandboxRuntimeConfig(enabled=True, backend="none", network="open")
        rt = build_runtime(cfg, get_cwd=lambda: str(tmp_path), guard_factory=lambda: guard)
        extra = tmp_path / "granted"
        extra.mkdir()
        guard.add_session_root(str(extra))
        policy = rt._policy_for(str(tmp_path))
        roots = [os.path.realpath(r) for r in policy.writable_roots]
        assert os.path.realpath(str(extra)) in roots

    def test_seccomp_flag_threads_through(self, tmp_path):
        cfg = SandboxRuntimeConfig(enabled=True, backend="none", network="open", seccomp=False)
        rt = build_runtime(cfg, get_cwd=lambda: str(tmp_path), guard_factory=lambda: _guard(str(tmp_path)))
        assert rt._seccomp is False

    def test_resource_limits_thread_through(self, tmp_path):
        cfg = SandboxRuntimeConfig(
            enabled=True,
            backend="none",
            network="open",
            memory_max="512M",
            pids_max=64,
            cpu_quota="200%",
        )
        rt = build_runtime(cfg, get_cwd=lambda: str(tmp_path), guard_factory=lambda: _guard(str(tmp_path)))
        # No explicit ResourceGuard -> build_runtime seeds a default one from the
        # config; the runtime reads it fresh via the wired limits_provider.
        limits = rt._current_limits()
        assert limits.memory_max == "512M"
        assert limits.pids_max == 64
        assert limits.cpu_quota == "200%"

    def test_resource_guard_adjustment_visible_next_call(self, tmp_path):
        from mote.executor.permission.sandbox import ResourceGuard

        cfg = SandboxRuntimeConfig(enabled=True, backend="none", network="open", memory_max="4G")
        rguard = ResourceGuard(cfg)
        rt = build_runtime(
            cfg,
            get_cwd=lambda: str(tmp_path),
            guard_factory=lambda: _guard(str(tmp_path)),
            resource_guard=rguard,
        )
        assert rt._current_limits().memory_max == "4G"
        # A session adjustment on the guard is honoured on the next read — the
        # dynamic analogue of SandboxGuard.add_session_root.
        rguard.set_memory_max("8G")
        assert rt._current_limits().memory_max == "8G"
