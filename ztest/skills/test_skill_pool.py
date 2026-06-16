#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for metagpt.context.skills.skill_pool.SkillPool."""
from __future__ import annotations

from pathlib import Path

from metagpt.context.skills.skill_pool import SkillPool

from .conftest import write_skill


class TestLoadByNames:
    def test_loads_requested_skill(self, builtin_dir):
        write_skill(builtin_dir, "alpha", description="Alpha skill")
        pool = SkillPool(builtin_dir=builtin_dir)
        pool.load_by_names(["alpha"])
        assert pool.get_skill_count() == 1
        assert pool.get_all()[0].name == "alpha"

    def test_loads_multiple(self, builtin_dir):
        write_skill(builtin_dir, "alpha")
        write_skill(builtin_dir, "beta")
        pool = SkillPool(builtin_dir=builtin_dir)
        pool.load_by_names(["alpha", "beta"])
        assert {s.name for s in pool.get_all()} == {"alpha", "beta"}

    def test_only_loads_requested_subset(self, builtin_dir):
        write_skill(builtin_dir, "alpha")
        write_skill(builtin_dir, "beta")
        pool = SkillPool(builtin_dir=builtin_dir)
        pool.load_by_names(["alpha"])
        assert {s.name for s in pool.get_all()} == {"alpha"}

    def test_missing_skill_skipped(self, builtin_dir):
        write_skill(builtin_dir, "alpha")
        pool = SkillPool(builtin_dir=builtin_dir)
        pool.load_by_names(["alpha", "does-not-exist"])
        assert pool.get_skill_count() == 1

    def test_empty_names_loads_nothing(self, builtin_dir):
        write_skill(builtin_dir, "alpha")
        pool = SkillPool(builtin_dir=builtin_dir)
        pool.load_by_names([])
        assert pool.get_skill_count() == 0

    def test_reload_clears_previous(self, builtin_dir):
        write_skill(builtin_dir, "alpha")
        write_skill(builtin_dir, "beta")
        pool = SkillPool(builtin_dir=builtin_dir)
        pool.load_by_names(["alpha", "beta"])
        assert pool.get_skill_count() == 2
        pool.load_by_names(["alpha"])
        assert pool.get_skill_count() == 1
        assert pool.get_all()[0].name == "alpha"


class TestScanAvailable:
    def test_skips_underscore_prefixed_parent_dir(self, builtin_dir):
        # Skill nested under an underscore dir must be ignored.
        write_skill(builtin_dir / "_internal", "hidden", dir_name="hidden")
        write_skill(builtin_dir, "visible")
        pool = SkillPool(builtin_dir=builtin_dir)
        pool.load_by_names(["hidden", "visible"])
        assert {s.name for s in pool.get_all()} == {"visible"}

    def test_nonexistent_builtin_dir_returns_empty(self, tmp_path):
        pool = SkillPool(builtin_dir=tmp_path / "nope")
        pool.load_by_names(["anything"])
        assert pool.get_skill_count() == 0

    def test_indexed_by_directory_name(self, builtin_dir):
        # _scan_available keys by directory name; load_by_names uses that key.
        write_skill(builtin_dir, "meta-name", dir_name="dir-name")
        pool = SkillPool(builtin_dir=builtin_dir)
        pool.load_by_names(["dir-name"])
        # found via dir name; the loaded skill keeps the metadata name
        assert pool.get_skill_count() == 1
        assert pool.get_all()[0].name == "meta-name"


class TestLoadSkillFromDir:
    def test_parses_all_metadata_fields(self, builtin_dir):
        write_skill(
            builtin_dir,
            "rich",
            description="Rich skill",
            always_apply=True,
            globs=["*.py", "*.md"],
            instructions="The instructions body.",
            extra_meta={"version": 2},
        )
        pool = SkillPool(builtin_dir=builtin_dir)
        pool.load_by_names(["rich"])
        s = pool.get_all()[0]
        assert s.name == "rich"
        assert s.description == "Rich skill"
        assert s.always_apply is True
        assert s.globs == ["*.py", "*.md"]
        assert s.instructions.strip() == "The instructions body."
        assert s.metadata["version"] == 2
        assert s.source_path.name == "SKILL.md"

    def test_name_falls_back_to_dir_when_meta_missing(self, builtin_dir):
        raw = "---\ndescription: No name in meta\n---\n\nBody."
        write_skill(builtin_dir, "ignored", dir_name="fallback-dir", raw=raw)
        pool = SkillPool(builtin_dir=builtin_dir)
        pool.load_by_names(["fallback-dir"])
        assert pool.get_skill_count() == 1
        assert pool.get_all()[0].name == "fallback-dir"

    def test_invalid_skill_no_description_rejected(self, builtin_dir):
        raw = "---\nname: nodesc\n---\n\nBody."
        write_skill(builtin_dir, "nodesc", raw=raw)
        pool = SkillPool(builtin_dir=builtin_dir)
        pool.load_by_names(["nodesc"])
        assert pool.get_skill_count() == 0

    def test_invalid_name_rejected(self, builtin_dir):
        # uppercase dir name -> invalid skill name -> rejected
        raw = "---\nname: BadName\ndescription: has desc\n---\n\nBody."
        write_skill(builtin_dir, "BadName", dir_name="BadName", raw=raw)
        pool = SkillPool(builtin_dir=builtin_dir)
        pool.load_by_names(["BadName"])
        assert pool.get_skill_count() == 0

    def test_empty_skill_md_rejected(self, builtin_dir):
        write_skill(builtin_dir, "empty", raw="")
        pool = SkillPool(builtin_dir=builtin_dir)
        pool.load_by_names(["empty"])
        assert pool.get_skill_count() == 0

    def test_missing_skill_md_file(self, builtin_dir):
        # Directory exists but has no SKILL.md -> not in scan, not loaded.
        (builtin_dir / "noskill").mkdir()
        pool = SkillPool(builtin_dir=builtin_dir)
        pool.load_by_names(["noskill"])
        assert pool.get_skill_count() == 0


class TestGetters:
    def test_get_all_returns_list(self, builtin_dir):
        write_skill(builtin_dir, "alpha")
        pool = SkillPool(builtin_dir=builtin_dir)
        pool.load_by_names(["alpha"])
        result = pool.get_all()
        assert isinstance(result, list)
        assert len(result) == 1

    def test_count_empty_pool(self, builtin_dir):
        pool = SkillPool(builtin_dir=builtin_dir)
        assert pool.get_skill_count() == 0
        assert pool.get_all() == []


def test_default_builtin_dir_is_package_dir():
    # No arg -> uses the packaged skills directory.
    pool = SkillPool()
    assert pool._builtin_dir == Path(__file__).parents[2] / "context" / "skills" / "yamls"
