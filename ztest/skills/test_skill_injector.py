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

    def test_loading_guide_points_at_skill_tool(self, builtin_dir):
        write_skill(builtin_dir, "alpha")
        injector = SkillInjector(pool=_pool(builtin_dir, ["alpha"]))
        content = injector.build_content()
        assert "Skill(name=" in content
        assert "Editor.read" not in content


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
        assert "| Skill | Description | Arguments |" in text
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

    def test_always_active_body_preserved_under_tight_budget(self, builtin_dir):
        # alwaysApply bodies are preserved in full even with a tiny budget.
        write_skill(builtin_dir, "auto", always_apply=True, instructions="KEEP THIS BODY")
        injector = SkillInjector(pool=_pool(builtin_dir, ["auto"]))
        content = injector.build_content(max_tokens=5)
        assert "KEEP THIS BODY" in content


class TestIndexDegradation:
    def _big_desc_pool(self, builtin_dir, n=5):
        pool = SkillPool(builtin_dir=builtin_dir)
        for i in range(n):
            pool._skills[f"s{i}"] = make_skill_def(
                name=f"s{i}", description="word " * 60, instructions="body"
            )
        return pool

    def test_tier0_full_description_when_budget_ample(self, builtin_dir):
        write_skill(builtin_dir, "alpha", description="A full clear description here")
        injector = SkillInjector(pool=_pool(builtin_dir, ["alpha"]))
        text = injector.build_content(max_tokens=5000)
        assert "A full clear description here" in text
        assert "| Skill | Description | Arguments |" in text

    def test_tier2_name_only_when_budget_tiny(self, builtin_dir):
        injector = SkillInjector(pool=self._big_desc_pool(builtin_dir))
        text = injector.build_content(max_tokens=30)
        # Name-only tier: no table header, just bullet list of names.
        assert "- s0" in text
        assert "| Skill | Description | Arguments |" not in text

    def test_tier1_half_description_mid_budget(self, builtin_dir):
        # A budget between full and name-only triggers the half-description tier.
        injector = SkillInjector(pool=self._big_desc_pool(builtin_dir, n=3))
        full = injector._build_index(injector._index_skills(), tier=0)
        half = injector._build_index(injector._index_skills(), tier=1)
        assert "…" in half
        assert len(half) < len(full)


class TestConditionalAndHiddenExcluded:
    def test_conditional_skill_excluded_from_index(self, builtin_dir):
        write_skill(builtin_dir, "cond", description="Conditional", extra_meta={"paths": ["*.py"]})
        write_skill(builtin_dir, "plain", description="Plain skill")
        injector = SkillInjector(pool=_pool(builtin_dir, ["cond", "plain"]))
        text = injector.build_content(max_tokens=5000)
        assert "plain" in text
        assert "cond" not in text

    def test_disable_model_invocation_excluded(self, builtin_dir):
        write_skill(
            builtin_dir, "hidden", description="Hidden", extra_meta={"disable_model_invocation": True}
        )
        write_skill(builtin_dir, "shown", description="Shown skill")
        injector = SkillInjector(pool=_pool(builtin_dir, ["hidden", "shown"]))
        text = injector.build_content(max_tokens=5000)
        assert "shown" in text
        assert "hidden" not in text
