#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for metagpt.skills.skill_definition.SkillDefinition."""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from metagpt.skills.skill_definition import NAME_PATTERN, SkillDefinition


class TestDefaults:
    def test_empty_defaults(self):
        s = SkillDefinition()
        assert s.name == ""
        assert s.description == ""
        assert s.always_apply is False
        assert s.globs == []
        assert s.roles == []
        assert s.instructions == ""
        assert s.source_path == Path()
        assert s.token_count == 0
        assert s.metadata == {}

    def test_mutable_defaults_are_independent(self):
        a = SkillDefinition()
        b = SkillDefinition()
        a.globs.append("*.py")
        a.roles.append("Engineer")
        a.metadata["k"] = 1
        assert b.globs == []
        assert b.roles == []
        assert b.metadata == {}


class TestTokenCount:
    def test_auto_counts_when_instructions_present(self):
        s = SkillDefinition(name="x", description="d", instructions="hello world foo bar")
        assert s.token_count > 0

    def test_no_count_when_instructions_empty(self):
        s = SkillDefinition(name="x", description="d", instructions="")
        assert s.token_count == 0

    def test_explicit_token_count_not_overridden(self):
        s = SkillDefinition(instructions="some long instructions here", token_count=999)
        assert s.token_count == 999

    def test_count_scales_with_length(self):
        short = SkillDefinition(instructions="hi")
        long = SkillDefinition(instructions="word " * 200)
        assert long.token_count > short.token_count


class TestIsValid:
    @pytest.mark.parametrize(
        "name",
        ["a", "skill", "my-skill", "skill-123", "abc-def-ghi", "x" * 64, "0", "1-2-3"],
    )
    def test_valid_names(self, name):
        assert SkillDefinition(name=name, description="d").is_valid()

    @pytest.mark.parametrize(
        "name",
        [
            "",  # empty
            "Skill",  # uppercase
            "MY-SKILL",  # uppercase
            "my_skill",  # underscore
            "my skill",  # space
            "my.skill",  # dot
            "my/skill",  # slash
            "skill!",  # punctuation
            "x" * 65,  # too long (max 64)
            "中文",  # non-ascii
        ],
    )
    def test_invalid_names(self, name):
        assert not SkillDefinition(name=name, description="d").is_valid()

    def test_invalid_when_no_description(self):
        assert not SkillDefinition(name="valid-name", description="").is_valid()

    def test_invalid_when_name_and_desc_empty(self):
        assert not SkillDefinition().is_valid()

    def test_valid_requires_both_name_and_desc(self):
        assert SkillDefinition(name="ok", description="present").is_valid()


class TestNamePattern:
    def test_pattern_matches_lowercase_alnum_hyphen(self):
        assert NAME_PATTERN.match("good-name-1")

    def test_pattern_rejects_empty(self):
        assert NAME_PATTERN.match("") is None

    def test_pattern_rejects_over_64(self):
        assert NAME_PATTERN.match("a" * 65) is None


class TestDescriptionMaxLength:
    def test_max_length_1024_ok(self):
        SkillDefinition(name="x", description="d" * 1024)  # no raise

    def test_over_max_length_raises(self):
        with pytest.raises(ValidationError):
            SkillDefinition(name="x", description="d" * 1025)
