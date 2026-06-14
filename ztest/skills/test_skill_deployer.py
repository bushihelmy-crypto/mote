#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for metagpt.skills.skill_deployer.SkillDeployer."""
from __future__ import annotations

from metagpt.common.const import ATOMS_DIR_NAME
from metagpt.skills.skill_deployer import SkillDeployer
from metagpt.skills.skill_pool import SkillPool

from .conftest import write_skill


def _load(builtin_dir, names):
    pool = SkillPool(builtin_dir=builtin_dir)
    pool.load_by_names(names)
    return pool.get_all()


class TestDeploy:
    def test_copies_skill_dir(self, builtin_dir, workspace):
        write_skill(builtin_dir, "alpha", instructions="Alpha body")
        skills = _load(builtin_dir, ["alpha"])
        deployed = SkillDeployer().deploy(workspace, skills)
        assert deployed == ["alpha"]
        dst = workspace / ATOMS_DIR_NAME / "skills" / "alpha" / "SKILL.md"
        assert dst.exists()
        assert "Alpha body" in dst.read_text(encoding="utf-8")

    def test_creates_target_dir(self, builtin_dir, workspace):
        write_skill(builtin_dir, "alpha")
        SkillDeployer().deploy(workspace, _load(builtin_dir, ["alpha"]))
        assert (workspace / ATOMS_DIR_NAME / "skills").is_dir()

    def test_deploys_multiple(self, builtin_dir, workspace):
        write_skill(builtin_dir, "alpha")
        write_skill(builtin_dir, "beta")
        deployed = SkillDeployer().deploy(workspace, _load(builtin_dir, ["alpha", "beta"]))
        assert set(deployed) == {"alpha", "beta"}

    def test_existing_dir_not_overwritten(self, builtin_dir, workspace):
        write_skill(builtin_dir, "alpha", instructions="NEW body")
        skills = _load(builtin_dir, ["alpha"])
        # Pre-create destination with different content.
        dst_dir = workspace / ATOMS_DIR_NAME / "skills" / "alpha"
        dst_dir.mkdir(parents=True)
        (dst_dir / "SKILL.md").write_text("OLD body", encoding="utf-8")

        deployed = SkillDeployer().deploy(workspace, skills)
        # Still reported as deployed, but content untouched.
        assert deployed == ["alpha"]
        assert (dst_dir / "SKILL.md").read_text(encoding="utf-8") == "OLD body"

    def test_empty_list_deploys_nothing(self, workspace):
        deployed = SkillDeployer().deploy(workspace, [])
        assert deployed == []
        assert (workspace / ATOMS_DIR_NAME / "skills").is_dir()

    def test_copies_auxiliary_files(self, builtin_dir, workspace):
        write_skill(builtin_dir, "alpha")
        (builtin_dir / "alpha" / "helper.py").write_text("print('hi')", encoding="utf-8")
        SkillDeployer().deploy(workspace, _load(builtin_dir, ["alpha"]))
        assert (workspace / ATOMS_DIR_NAME / "skills" / "alpha" / "helper.py").exists()


class TestGenerateIndex:
    def test_creates_skills_md(self, builtin_dir, workspace):
        write_skill(builtin_dir, "alpha", description="Alpha desc")
        skills = _load(builtin_dir, ["alpha"])
        path = SkillDeployer().generate_index(workspace, skills)
        assert path == workspace / ATOMS_DIR_NAME / "SKILLS.md"
        assert path.exists()

    def test_index_has_frontmatter_and_header(self, builtin_dir, workspace):
        write_skill(builtin_dir, "alpha", description="Alpha desc")
        path = SkillDeployer().generate_index(workspace, _load(builtin_dir, ["alpha"]))
        text = path.read_text(encoding="utf-8")
        assert text.startswith("---")
        assert "auto_generated: true" in text
        assert "last_updated:" in text
        assert "# Available Skills" in text
        assert "| Skill | Description | Path |" in text

    def test_index_row_per_skill(self, builtin_dir, workspace):
        write_skill(builtin_dir, "alpha", description="Alpha desc")
        write_skill(builtin_dir, "beta", description="Beta desc")
        path = SkillDeployer().generate_index(workspace, _load(builtin_dir, ["alpha", "beta"]))
        text = path.read_text(encoding="utf-8")
        assert "| alpha | Alpha desc |" in text
        assert "| beta | Beta desc |" in text
        # absolute path pointing at deployed SKILL.md
        assert str(workspace / ATOMS_DIR_NAME / "skills" / "alpha" / "SKILL.md") in text

    def test_description_pipe_escaped(self, builtin_dir, workspace):
        write_skill(builtin_dir, "alpha", description="has | pipe")
        path = SkillDeployer().generate_index(workspace, _load(builtin_dir, ["alpha"]))
        text = path.read_text(encoding="utf-8")
        assert r"has \| pipe" in text

    def test_description_newline_flattened(self, builtin_dir, workspace):
        write_skill(builtin_dir, "alpha", description="line one\nline two")
        path = SkillDeployer().generate_index(workspace, _load(builtin_dir, ["alpha"]))
        text = path.read_text(encoding="utf-8")
        assert "| alpha | line one line two |" in text

    def test_empty_skills_still_writes_index(self, workspace):
        path = SkillDeployer().generate_index(workspace, [])
        text = path.read_text(encoding="utf-8")
        assert "# Available Skills" in text
        # header row present, no data rows
        assert "| Skill | Description | Path |" in text
