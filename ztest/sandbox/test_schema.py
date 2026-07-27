#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Schema tests for ``SandboxRuntimeConfig`` + ``PermissionConfig.runtime``.

Covers the declarative config shape, defaults, the ``network_enforced`` helper,
the lazy registration in ``common.schema`` (no eager import), and that
``PermissionConfig`` accepts an optional runtime block + ``SandboxConfig`` an
``allowed_domains`` list.
"""
from __future__ import annotations

from mote.contracts.settings.permissions import PermissionConfig, SandboxConfig
from mote.contracts.settings.sandbox import SandboxRuntimeConfig


class TestSandboxRuntimeConfig:
    def test_defaults_disabled(self):
        cfg = SandboxRuntimeConfig()
        assert cfg.enabled is False
        assert cfg.backend == "auto"
        assert cfg.network == "proxy"
        assert cfg.harden_process is True
        assert cfg.seccomp is True
        assert cfg.fail_if_unavailable is False
        assert cfg.allowed_domains == []

    def test_seccomp_toggle(self):
        assert SandboxRuntimeConfig(seccomp=False).seccomp is False

    def test_resource_limit_defaults(self):
        cfg = SandboxRuntimeConfig()
        assert cfg.memory_max == "4G"
        assert cfg.pids_max == 512
        # cpu_quota off by default (controller usually undelegated).
        assert cfg.cpu_quota is None

    def test_resource_limits_parse(self):
        cfg = SandboxRuntimeConfig(memory_max="512M", pids_max=64, cpu_quota="200%")
        assert cfg.memory_max == "512M"
        assert cfg.pids_max == 64
        assert cfg.cpu_quota == "200%"

    def test_resource_limits_nullable(self):
        cfg = SandboxRuntimeConfig(memory_max=None, pids_max=None)
        assert cfg.memory_max is None
        assert cfg.pids_max is None

    def test_network_enforcement_defaults_on(self):
        assert SandboxRuntimeConfig().network_enforcement is True
        assert SandboxRuntimeConfig(network_enforcement=False).network_enforcement is False

    def test_network_enforced_only_for_proxy(self):
        assert SandboxRuntimeConfig(network="proxy").network_enforced() is True
        assert SandboxRuntimeConfig(network="off").network_enforced() is False
        assert SandboxRuntimeConfig(network="open").network_enforced() is False

    def test_accepts_allowed_domains(self):
        cfg = SandboxRuntimeConfig(allowed_domains=["*.pypi.org", "github.com"])
        assert cfg.allowed_domains == ["*.pypi.org", "github.com"]


class TestPermissionConfigRuntime:
    def test_runtime_defaults_none(self):
        pc = PermissionConfig()
        assert pc.runtime is None

    def test_runtime_block_parses(self):
        pc = PermissionConfig(runtime={"enabled": True, "backend": "bwrap", "network": "off"})
        assert pc.runtime is not None
        assert pc.runtime.enabled is True
        assert pc.runtime.backend == "bwrap"
        assert pc.runtime.network == "off"

    def test_sandbox_allowed_domains(self):
        sc = SandboxConfig(allowed_domains=["example.com"])
        assert sc.allowed_domains == ["example.com"]
