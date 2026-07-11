#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for mote.prompts.memory — the auto-memory prompt split.

Two contracts matter: (1) MEMORY_INSTRUCTIONS carries exactly the ${memory_dir}
placeholder (static system section) and MEMORY_CONTEXT carries exactly
${memory_content} (dynamic user-context block); (2) both render cleanly via
string.Template.safe_substitute. The four memory types and the NOT-to-save
rules are pinned so the behavioural taxonomy can't silently drift.
"""
from __future__ import annotations

from string import Template

from mote.common.prompt import memory as M


class TestInstructions:
    def test_has_memory_dir_placeholder(self):
        assert "${memory_dir}" in M.MEMORY_INSTRUCTIONS

    def test_substitutes_memory_dir(self):
        out = Template(M.MEMORY_INSTRUCTIONS).safe_substitute(memory_dir="/tmp/mem/")
        assert "/tmp/mem/" in out
        assert "${memory_dir}" not in out

    def test_literal_braces_survive_substitution(self):
        # The frontmatter example uses single {...} placeholders (display only).
        # Template only treats $-prefixed names as fields, so safe_substitute
        # leaves the literal braces alone and never raises on them.
        out = Template(M.MEMORY_INSTRUCTIONS).safe_substitute(memory_dir="/x")
        assert "{memory name}" in out

    def test_lists_four_memory_types(self):
        for t in ("user:", "feedback:", "project:", "reference:"):
            assert t in M.MEMORY_INSTRUCTIONS, t

    def test_has_not_to_save_section(self):
        assert "What NOT to save" in M.MEMORY_INSTRUCTIONS


class TestContext:
    def test_has_memory_content_placeholder(self):
        assert "${memory_content}" in M.MEMORY_CONTEXT

    def test_substitutes_content(self):
        out = Template(M.MEMORY_CONTEXT).safe_substitute(memory_content="- [A](a.md) — hook")
        assert "- [A](a.md) — hook" in out
        assert out.startswith("# MEMORY.md")

    def test_empty_state_substitutes(self):
        out = Template(M.MEMORY_CONTEXT).safe_substitute(memory_content=M.MEMORY_EMPTY_STATE)
        assert M.MEMORY_EMPTY_STATE in out


class TestConstants:
    def test_empty_state_nonempty(self):
        assert M.MEMORY_EMPTY_STATE.strip()

    def test_frontmatter_example_has_fields(self):
        for field in ("name:", "description:", "type:"):
            assert field in M.MEMORY_FRONTMATTER_EXAMPLE, field
