#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the :class:`ResourceGuard` (``executor.permission.sandbox.resource_guard``).

The cgroup analogue of ``SandboxGuard``: seeded from a ``SandboxRuntimeConfig``,
exposing live :class:`ResourceLimits` via ``limits()`` plus session setters that
take effect on the next read (the dynamic-cap contract the runtime relies on).
"""

from __future__ import annotations

from mote.runtime.sandbox.config import SandboxRuntimeConfig
from mote.runtime.tools.permission.sandbox.resource_guard import ResourceGuard


def _cfg(**kw) -> SandboxRuntimeConfig:
    base = dict(profile="isolated-compute", network="off")
    base.update(kw)
    return SandboxRuntimeConfig(**base)


class TestSeed:
    def test_seeds_from_config(self):
        guard = ResourceGuard(_cfg(memory_max="512M", pids_max=64, cpu_quota="200%"))
        limits = guard.limits()
        assert limits.memory_max == "512M"
        assert limits.pids_max == 64
        assert limits.cpu_quota == "200%"

    def test_defaults_carry_through(self):
        # SandboxRuntimeConfig defaults: memory_max="4G", pids_max=512, cpu_quota=None.
        guard = ResourceGuard(_cfg())
        limits = guard.limits()
        assert limits.memory_max == "4G"
        assert limits.pids_max == 512
        assert limits.cpu_quota is None

    def test_swap_disabled_by_default(self):
        # ResourceLimits defaults memory_swap_max="0" so a capped payload cannot
        # sidestep the RSS cap via swap.
        assert ResourceGuard(_cfg(memory_max="1G")).limits().memory_swap_max == "0"


class TestSetters:
    def test_set_memory_max_visible_next_read(self):
        guard = ResourceGuard(_cfg(memory_max="4G"))
        guard.set_memory_max("8G")
        assert guard.limits().memory_max == "8G"

    def test_set_pids_max(self):
        guard = ResourceGuard(_cfg(pids_max=512))
        guard.set_pids_max(16)
        assert guard.limits().pids_max == 16

    def test_set_cpu_quota(self):
        guard = ResourceGuard(_cfg())
        guard.set_cpu_quota("150%")
        assert guard.limits().cpu_quota == "150%"

    def test_setters_accept_none_to_uncap(self):
        guard = ResourceGuard(_cfg(memory_max="4G", pids_max=512))
        guard.set_memory_max(None)
        guard.set_pids_max(None)
        limits = guard.limits()
        assert limits.memory_max is None
        assert limits.pids_max is None

    def test_limits_returns_same_live_object(self):
        # The runtime caches nothing — each limits() read sees prior mutations.
        guard = ResourceGuard(_cfg(memory_max="4G"))
        first = guard.limits()
        guard.set_memory_max("2G")
        assert first.memory_max == "2G"  # same underlying object, mutated in place
