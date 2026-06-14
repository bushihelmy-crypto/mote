#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for metagpt.skills.skill_injector.SkillInjector."""
from __future__ import annotations

from metagpt.skills.skill_injector import SkillInjector
from metagpt.skills.skill_pool import SkillPool

from .conftest import make_skill_def, write_skill


def _pool(builtin_dir, names):
    pool = SkillPool(builtin_dir=builtin_dir)
    pool.load_by_names(names)
    return pool


def _index_file(tmp_path, body="| alpha | Alpha desc | /x/SKILL.md |"):
    p = tmp_path / "SKILLS.md"
    p.write_text(
        "---\nauto_generated: true\n---\n\n# Available Skills\n\n" + body + "\n",
        encoding="utf-8",
    )
    return p


class TestBuildContentEmpty:
    def test_empty_when_no_skills(self, builtin_dir):
        injector = SkillInjector(pool=_pool(builtin_dir, []))
        assert injector.build_content() == ""

    def test_inject_returns_prompt_unchanged_when_empty(self, builtin_dir):
        injector = SkillInjector(pool=_pool(builtin_dir, []))
        assert injector.inject("BASE") == "BASE"


class TestBuildContentIndex:
    def test_includes_index_section(self, builtin_dir, tmp_path):
        write_skill(builtin_dir, "alpha")
        injector = SkillInjector(pool=_pool(builtin_dir, ["alpha"]), skills_md_path=_index_file(tmp_path))
        content = injector.build_content()
        assert "## Available Skills" in content
        assert "alpha" in content

    def test_includes_loading_guide(self, builtin_dir, tmp_path):
        write_skill(builtin_dir, "alpha")
        injector = SkillInjector(pool=_pool(builtin_dir, ["alpha"]), skills_md_path=_index_file(tmp_path))
        assert "Skill Loading Guide" in injector.build_content()

    def test_no_index_section_when_path_missing(self, builtin_dir):
        write_skill(builtin_dir, "alpha")
        injector = SkillInjector(pool=_pool(builtin_dir, ["alpha"]), skills_md_path=None)
        content = injector.build_content()
        # loading guide still present, but no index section
        assert "Skill Loading Guide" in content
        assert "## Available Skills" not in content


class TestAlwaysActive:
    def test_always_apply_skill_included(self, builtin_dir):
        write_skill(builtin_dir, "auto", always_apply=True, instructions="AUTO INSTRUCTIONS")
        injector = SkillInjector(pool=_pool(builtin_dir, ["auto"]))
        content = injector.build_content()
        assert "## Always Active Skills" in content
        assert "### auto" in content
        assert "AUTO INSTRUCTIONS" in content

    def test_non_always_apply_excluded_from_active(self, builtin_dir):
        write_skill(builtin_dir, "manual", always_apply=False, instructions="MANUAL INSTRUCTIONS")
        injector = SkillInjector(pool=_pool(builtin_dir, ["manual"]))
        content = injector.build_content()
        assert "## Always Active Skills" not in content
        assert "MANUAL INSTRUCTIONS" not in content

    def test_mixed_only_always_apply_in_active(self, builtin_dir):
        write_skill(builtin_dir, "auto", always_apply=True, instructions="AUTO BODY")
        write_skill(builtin_dir, "manual", always_apply=False, instructions="MANUAL BODY")
        injector = SkillInjector(pool=_pool(builtin_dir, ["auto", "manual"]))
        content = injector.build_content()
        assert "AUTO BODY" in content
        assert "MANUAL BODY" not in content


class TestInject:
    def test_appends_to_prompt(self, builtin_dir):
        write_skill(builtin_dir, "auto", always_apply=True)
        injector = SkillInjector(pool=_pool(builtin_dir, ["auto"]))
        result = injector.inject("BASE PROMPT")
        assert result.startswith("BASE PROMPT\n\n")
        assert "Always Active Skills" in result


class TestReadSkillsIndex:
    def test_strips_frontmatter(self, builtin_dir, tmp_path):
        write_skill(builtin_dir, "alpha")
        idx = _index_file(tmp_path)
        injector = SkillInjector(pool=_pool(builtin_dir, ["alpha"]), skills_md_path=idx)
        text = injector._read_skills_index()
        assert not text.startswith("---")
        assert "auto_generated" not in text
        assert "# Available Skills" in text

    def test_returns_empty_when_path_none(self, builtin_dir):
        injector = SkillInjector(pool=_pool(builtin_dir, []), skills_md_path=None)
        assert injector._read_skills_index() == ""

    def test_returns_empty_when_file_missing(self, builtin_dir, tmp_path):
        injector = SkillInjector(pool=_pool(builtin_dir, []), skills_md_path=tmp_path / "nope.md")
        assert injector._read_skills_index() == ""

    def test_no_frontmatter_kept_as_is(self, builtin_dir, tmp_path):
        p = tmp_path / "SKILLS.md"
        p.write_text("# Just A Heading\n\nbody", encoding="utf-8")
        injector = SkillInjector(pool=_pool(builtin_dir, []), skills_md_path=p)
        assert injector._read_skills_index() == "# Just A Heading\n\nbody"


class TestSanitizeAndTruncate:
    def test_dangerous_patterns_stripped_from_instructions(self, builtin_dir):
        write_skill(
            builtin_dir,
            "auto",
            always_apply=True,
            instructions="before <system>evil</system> after",
        )
        injector = SkillInjector(pool=_pool(builtin_dir, ["auto"]))
        content = injector.build_content()
        assert "<system>" not in content
        assert "</system>" not in content
        assert "before" in content and "after" in content

    def test_truncation_when_over_max_tokens(self, builtin_dir):
        # Build a pool with a large always-apply skill via direct injection.
        pool = SkillPool(builtin_dir=builtin_dir)
        big = make_skill_def(
            name="big", description="d", always_apply=True, instructions="word " * 5000
        )
        pool._skills["big"] = big
        injector = SkillInjector(pool=pool)
        content = injector.build_content(max_tokens=50)
        assert "[... truncated due to token limit]" in content

    def test_no_truncation_when_under_max_tokens(self, builtin_dir):
        write_skill(builtin_dir, "auto", always_apply=True, instructions="short body")
        injector = SkillInjector(pool=_pool(builtin_dir, ["auto"]))
        content = injector.build_content(max_tokens=2000)
        assert "[... truncated due to token limit]" not in content
