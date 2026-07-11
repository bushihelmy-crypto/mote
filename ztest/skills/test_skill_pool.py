#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for mote.context.skills.skill_pool.SkillPool."""
from __future__ import annotations

from pathlib import Path

from mote.context.skills.skill_pool import SkillPool

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
            globs=["*.py", "*.md"],
            instructions="The instructions body.",
            extra_meta={"version": 2},
        )
        pool = SkillPool(builtin_dir=builtin_dir)
        pool.load_by_names(["rich"])
        s = pool.get_all()[0]
        assert s.name == "rich"
        assert s.description == "Rich skill"
        assert s.globs == ["*.py", "*.md"]
        assert s.instructions.strip() == "The instructions body."
        assert s.metadata["version"] == 2
        assert s.source_path.name == "SKILL.md"

    def test_parses_extended_frontmatter(self, builtin_dir):
        write_skill(
            builtin_dir,
            "rich2",
            description="Rich2",
            extra_meta={
                "when-to-use": "when doing X",
                "context": "fork",
                "allowed-tools": ["Read", "Glob"],
                "model": "claude-opus-4-6",
                "effort": "high",
                "argument-hint": "<arg>",
                "disable_model_invocation": True,
                "paths": ["**/*.md"],
            },
        )
        pool = SkillPool(builtin_dir=builtin_dir)
        pool.load_by_names(["rich2"])
        s = pool.get_all()[0]
        assert s.when_to_use == "when doing X"
        assert s.context == "fork"
        assert s.allowed_tools == ["Read", "Glob"]
        assert s.model == "claude-opus-4-6"
        assert s.effort == "high"
        assert s.argument_hint == "<arg>"
        assert s.disable_model_invocation is True
        assert s.paths == ["**/*.md"]

    def test_invalid_context_falls_back_inline(self, builtin_dir):
        write_skill(builtin_dir, "ctx", description="d", extra_meta={"context": "weird"})
        pool = SkillPool(builtin_dir=builtin_dir)
        pool.load_by_names(["ctx"])
        assert pool.get_all()[0].context == "inline"

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
    assert pool.builtin_dir == Path(__file__).parents[2] / "context" / "skills" / "yamls"
    assert pool.source_dirs == [Path(__file__).parents[2] / "context" / "skills" / "yamls"]


class TestLoadAll:
    def test_loads_everything_discovered(self, builtin_dir):
        write_skill(builtin_dir, "alpha")
        write_skill(builtin_dir, "beta")
        pool = SkillPool(builtin_dir=builtin_dir)
        pool.load_all()
        assert {s.name for s in pool.get_all()} == {"alpha", "beta"}

    def test_get_returns_skill_or_none(self, builtin_dir):
        write_skill(builtin_dir, "alpha")
        pool = SkillPool(builtin_dir=builtin_dir)
        pool.load_all()
        assert pool.get("alpha").name == "alpha"
        assert pool.get("missing") is None


class TestLayeredSources:
    def test_higher_layer_overrides_same_name(self, tmp_path):
        low = tmp_path / "low"
        high = tmp_path / "high"
        low.mkdir()
        high.mkdir()
        write_skill(low, "dup", description="LOW")
        write_skill(high, "dup", description="HIGH")
        pool = SkillPool(source_dirs=[low, high])  # high overrides low
        pool.load_all()
        assert pool.get_skill_count() == 1
        assert pool.get("dup").description == "HIGH"

    def test_union_of_distinct_skills(self, tmp_path):
        low = tmp_path / "low"
        high = tmp_path / "high"
        low.mkdir()
        high.mkdir()
        write_skill(low, "alpha")
        write_skill(high, "beta")
        pool = SkillPool(source_dirs=[low, high])
        pool.load_all()
        assert {s.name for s in pool.get_all()} == {"alpha", "beta"}

    def test_missing_dir_is_skipped(self, tmp_path):
        present = tmp_path / "present"
        present.mkdir()
        write_skill(present, "alpha")
        pool = SkillPool(source_dirs=[tmp_path / "nope", present])
        pool.load_all()
        assert {s.name for s in pool.get_all()} == {"alpha"}

    def test_same_dir_listed_twice_deduped(self, builtin_dir):
        write_skill(builtin_dir, "alpha")
        pool = SkillPool(source_dirs=[builtin_dir, builtin_dir])
        pool.load_all()
        assert pool.get_skill_count() == 1

    def test_source_dirs_exposes_all_layers(self, tmp_path):
        low = tmp_path / "low"
        high = tmp_path / "high"
        low.mkdir()
        high.mkdir()
        pool = SkillPool(source_dirs=[low, high])
        assert pool.source_dirs == [low, high]
        assert pool.builtin_dir == low
