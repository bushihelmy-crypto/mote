#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for metagpt.context.skills.skill_manager.SkillManager.

ensure_ready() builds a default SkillPool (which reads skill_pool._BUILTIN_DIR),
so that constant is monkeypatched at the module where it is *looked up*. Skills
are read directly from the builtin dir; nothing is copied to disk.
"""
from __future__ import annotations

import metagpt.context.skills.skill_pool as sp_mod
from metagpt.context.skills.skill_manager import SkillManager

from .conftest import write_skill


def _patch_builtin(monkeypatch, builtin_dir):
    monkeypatch.setattr(sp_mod, "_BUILTIN_DIR", builtin_dir)


class TestDisabled:
    def test_empty_skills_ready_no_pool(self):
        mgr = SkillManager([])
        assert mgr.ready is False
        mgr.ensure_ready()
        assert mgr.ready is True
        assert mgr.pool is None
        assert mgr.injector is None

    def test_idempotent_when_disabled(self):
        mgr = SkillManager([])
        mgr.ensure_ready()
        mgr.ensure_ready()  # no raise, stays ready
        assert mgr.ready is True


class TestEnabled:
    def test_loads_and_wires_injector(self, monkeypatch, builtin_dir):
        write_skill(builtin_dir, "alpha", description="Alpha desc")
        _patch_builtin(monkeypatch, builtin_dir)

        mgr = SkillManager(["alpha"])
        mgr.ensure_ready()

        assert mgr.ready is True
        assert mgr.pool is not None
        assert mgr.pool.get_skill_count() == 1
        assert mgr.injector is not None

    def test_injector_builds_content(self, monkeypatch, builtin_dir):
        write_skill(builtin_dir, "auto", description="Auto", always_apply=True, instructions="AUTO")
        _patch_builtin(monkeypatch, builtin_dir)

        mgr = SkillManager(["auto"])
        mgr.ensure_ready()
        content = mgr.injector.build_content()
        assert "## Available Skills" in content
        assert "AUTO" in content

    def test_idempotent_when_enabled(self, monkeypatch, builtin_dir):
        write_skill(builtin_dir, "alpha")
        _patch_builtin(monkeypatch, builtin_dir)
        mgr = SkillManager(["alpha"])
        mgr.ensure_ready()
        pool_first = mgr.pool
        mgr.ensure_ready()  # second call short-circuits
        assert mgr.pool is pool_first


class TestReload:
    def test_reload_noop_before_ready(self, monkeypatch, builtin_dir):
        write_skill(builtin_dir, "alpha")
        _patch_builtin(monkeypatch, builtin_dir)
        mgr = SkillManager(["alpha"])
        assert mgr.reload() is False  # not initialized yet
        assert mgr.pool is None

    def test_reload_noop_when_disabled(self):
        mgr = SkillManager([])
        mgr.ensure_ready()
        assert mgr.reload() is False  # no skills configured

    def test_reload_swaps_pool_and_injector(self, monkeypatch, builtin_dir):
        write_skill(builtin_dir, "alpha", description="v1")
        _patch_builtin(monkeypatch, builtin_dir)
        mgr = SkillManager(["alpha"])
        mgr.ensure_ready()
        pool_first, injector_first = mgr.pool, mgr.injector

        assert mgr.reload() is True
        assert mgr.pool is not pool_first  # atomic swap to fresh objects
        assert mgr.injector is not injector_first
        assert mgr.pool.get_skill_count() == 1

    def test_reload_picks_up_new_skill_content(self, monkeypatch, builtin_dir):
        write_skill(builtin_dir, "auto", always_apply=True, instructions="OLD")
        _patch_builtin(monkeypatch, builtin_dir)
        mgr = SkillManager(["auto"])
        mgr.ensure_ready()
        assert "OLD" in mgr.injector.build_content()

        write_skill(builtin_dir, "auto", always_apply=True, instructions="NEW")  # edit on disk
        assert mgr.reload() is True
        assert "NEW" in mgr.injector.build_content()


class TestSourceDirs:
    def test_source_dirs_reports_builtin_dir(self, monkeypatch, builtin_dir):
        _patch_builtin(monkeypatch, builtin_dir)
        mgr = SkillManager(["alpha"])
        assert mgr.source_dirs() == [str(builtin_dir)]  # before ensure_ready

    def test_source_dirs_uses_loaded_pool_after_ready(self, monkeypatch, builtin_dir):
        write_skill(builtin_dir, "alpha")
        _patch_builtin(monkeypatch, builtin_dir)
        mgr = SkillManager(["alpha"])
        mgr.ensure_ready()
        assert mgr.source_dirs() == [str(builtin_dir)]
