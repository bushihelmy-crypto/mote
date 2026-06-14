#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for metagpt.skills.skill_manager.SkillManager.

ensure_ready() reads the module-level DEFAULT_WORKSPACE_ROOT and builds a
default SkillPool (which reads skill_pool._BUILTIN_DIR), so both constants are
monkeypatched at the modules where they are *looked up*.
"""
from __future__ import annotations

import metagpt.skills.skill_manager as sm_mod
import metagpt.skills.skill_pool as sp_mod
from metagpt.common.const import ATOMS_DIR_NAME
from metagpt.skills.skill_manager import SkillManager

from .conftest import write_skill


def _patch_dirs(monkeypatch, builtin_dir, workspace):
    monkeypatch.setattr(sp_mod, "_BUILTIN_DIR", builtin_dir)
    monkeypatch.setattr(sm_mod, "DEFAULT_WORKSPACE_ROOT", workspace)


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
    def test_loads_and_deploys(self, monkeypatch, builtin_dir, workspace):
        write_skill(builtin_dir, "alpha", description="Alpha desc")
        _patch_dirs(monkeypatch, builtin_dir, workspace)

        mgr = SkillManager(["alpha"])
        mgr.ensure_ready()

        assert mgr.ready is True
        assert mgr.pool is not None
        assert mgr.pool.get_skill_count() == 1
        assert mgr.injector is not None
        # deployed + index written
        assert (workspace / ATOMS_DIR_NAME / "skills" / "alpha" / "SKILL.md").exists()
        assert (workspace / ATOMS_DIR_NAME / "SKILLS.md").exists()

    def test_injector_wired_to_index(self, monkeypatch, builtin_dir, workspace):
        write_skill(builtin_dir, "auto", description="Auto", always_apply=True, instructions="AUTO")
        _patch_dirs(monkeypatch, builtin_dir, workspace)

        mgr = SkillManager(["auto"])
        mgr.ensure_ready()
        content = mgr.injector.build_content()
        assert "## Available Skills" in content
        assert "AUTO" in content

    def test_idempotent_when_enabled(self, monkeypatch, builtin_dir, workspace):
        write_skill(builtin_dir, "alpha")
        _patch_dirs(monkeypatch, builtin_dir, workspace)
        mgr = SkillManager(["alpha"])
        mgr.ensure_ready()
        pool_first = mgr.pool
        mgr.ensure_ready()  # second call short-circuits
        assert mgr.pool is pool_first

    def test_skips_deploy_when_already_present(self, monkeypatch, builtin_dir, workspace):
        write_skill(builtin_dir, "alpha", instructions="ORIGINAL")
        _patch_dirs(monkeypatch, builtin_dir, workspace)

        # Pre-populate workspace as if a prior deploy happened.
        skills_dir = workspace / ATOMS_DIR_NAME / "skills" / "alpha"
        skills_dir.mkdir(parents=True)
        (skills_dir / "SKILL.md").write_text("PRESERVED", encoding="utf-8")
        (workspace / ATOMS_DIR_NAME / "SKILLS.md").write_text("OLD INDEX", encoding="utf-8")

        mgr = SkillManager(["alpha"])
        mgr.ensure_ready()
        # needs_deploy is False -> deployed content + index untouched
        assert (skills_dir / "SKILL.md").read_text(encoding="utf-8") == "PRESERVED"
        assert (workspace / ATOMS_DIR_NAME / "SKILLS.md").read_text(encoding="utf-8") == "OLD INDEX"

    def test_redeploys_when_skill_dir_missing(self, monkeypatch, builtin_dir, workspace):
        write_skill(builtin_dir, "alpha")
        _patch_dirs(monkeypatch, builtin_dir, workspace)
        # Index exists but the skill dir is absent -> needs_deploy True.
        (workspace / ATOMS_DIR_NAME).mkdir(parents=True)
        (workspace / ATOMS_DIR_NAME / "SKILLS.md").write_text("stale", encoding="utf-8")

        mgr = SkillManager(["alpha"])
        mgr.ensure_ready()
        assert (workspace / ATOMS_DIR_NAME / "skills" / "alpha" / "SKILL.md").exists()


class TestWorkspaceMissing:
    def test_no_deploy_when_workspace_absent(self, monkeypatch, builtin_dir, tmp_path):
        write_skill(builtin_dir, "alpha")
        missing_ws = tmp_path / "does-not-exist"
        _patch_dirs(monkeypatch, builtin_dir, missing_ws)

        mgr = SkillManager(["alpha"])
        mgr.ensure_ready()
        # pool loaded but injector not created (returns early)
        assert mgr.ready is True
        assert mgr.pool is not None
        assert mgr.pool.get_skill_count() == 1
        assert mgr.injector is None
        assert not missing_ws.exists()
