#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for metagpt.context.skills.skill_injector.SkillInjector."""
from __future__ import annotations

from metagpt.context.skills.skill_injector import SkillInjector
from metagpt.context.skills.skill_pool import SkillPool

from .conftest import make_skill_def, write_skill


def _pool(builtin_dir, names):
    pool = SkillPool(builtin_dir=builtin_dir)
    pool.load_by_names(names)
    return pool


class TestBuildContentEmpty:
    def test_empty_when_no_skills(self, builtin_dir):
        injector = SkillInjector(pool=_pool(builtin_dir, []))
        assert injector.build_content() == ""

    def test_inject_returns_prompt_unchanged_when_empty(self, builtin_dir):
        injector = SkillInjector(pool=_pool(builtin_dir, []))
        assert injector.inject("BASE") == "BASE"


class TestBuildContentIndex:
    def test_includes_index_section(self, builtin_dir):
        write_skill(builtin_dir, "alpha")
        injector = SkillInjector(pool=_pool(builtin_dir, ["alpha"]))
        content = injector.build_content()
        assert "## Available Skills" in content
        assert "alpha" in content

    def test_includes_loading_guide(self, builtin_dir):
        write_skill(builtin_dir, "alpha")
        injector = SkillInjector(pool=_pool(builtin_dir, ["alpha"]))
        assert "Skill Loading Guide" in injector.build_content()

    def test_index_path_points_at_source_skill_md(self, builtin_dir):
        skill_md = write_skill(builtin_dir, "alpha")
        injector = SkillInjector(pool=_pool(builtin_dir, ["alpha"]))
        content = injector.build_content()
        # The on-demand load path points at the builtin source SKILL.md.
        assert str(skill_md) in content


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


class TestBuildIndex:
    def test_empty_when_no_skills(self, builtin_dir):
        injector = SkillInjector(pool=_pool(builtin_dir, []))
        assert injector._build_index() == ""

    def test_table_lists_each_skill(self, builtin_dir):
        write_skill(builtin_dir, "alpha", description="Alpha desc")
        write_skill(builtin_dir, "beta", description="Beta desc")
        injector = SkillInjector(pool=_pool(builtin_dir, ["alpha", "beta"]))
        text = injector._build_index()
        assert "| Skill | Description | Path |" in text
        assert "alpha" in text and "Alpha desc" in text
        assert "beta" in text and "Beta desc" in text

    def test_escapes_pipe_in_description(self, builtin_dir):
        write_skill(builtin_dir, "alpha", description="a | b")
        injector = SkillInjector(pool=_pool(builtin_dir, ["alpha"]))
        assert r"a \| b" in injector._build_index()


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
