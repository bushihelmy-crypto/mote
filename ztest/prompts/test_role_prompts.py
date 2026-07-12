#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for mote.prompts.role — the static template/constant catalogue.

These are not behaviours but contracts: the streaming parser, PromptBuilder and
the cache-boundary split all depend on the exact placeholder names and the
position of SYSTEM_PROMPT_DYNAMIC_BOUNDARY. The tests pin those contracts so a
careless edit to a template string is caught.
"""
from __future__ import annotations

import re
from string import Template

from mote.common import prompt as R


class TestSystemPromptBoundary:
    def test_boundary_marker_present(self):
        assert R.SYSTEM_PROMPT_DYNAMIC_BOUNDARY in R.SYSTEM_PROMPT

    def test_boundary_substituted_in(self):
        # SYSTEM_PROMPT is built by .replace("{boundary}", ...), so the literal
        # "{boundary}" token must be gone.
        assert "{boundary}" not in R.SYSTEM_PROMPT

    def test_no_placeholders_above_boundary(self):
        """The cacheable prefix must be byte-stable: no ${...} above the marker."""
        prefix = R.SYSTEM_PROMPT.split(R.SYSTEM_PROMPT_DYNAMIC_BOUNDARY)[0]
        assert not re.search(r"\$\{", prefix)

    def test_expected_placeholders_below_boundary(self):
        below = R.SYSTEM_PROMPT.split(R.SYSTEM_PROMPT_DYNAMIC_BOUNDARY)[1]
        for ph in (
            "${command_guide}",
            "${tool_usage_guide}",
            "${memory}",
            "${language}",
            "${scratchpad}",
            "${env_section}",
            "${skills_info}",
            "${frc}",
            "${task_final_output}",
        ):
            assert ph in below, ph


class TestDynamicSectionPlaceholders:
    def test_language_section(self):
        assert "${language_name}" in R.LANGUAGE_SECTION

    def test_scratchpad_section(self):
        assert "${scratchpad_dir}" in R.SCRATCHPAD_SECTION

    def test_frc_section(self):
        assert "${keep_recent}" in R.FRC_SECTION


class TestAgentPrompts:
    def test_agent_task_prompt_formats(self):
        out = Template(R.AGENT_TASK_PROMPT).safe_substitute(parent_name="Mike", context="ctx", task="do it")
        assert "Mike" in out
        assert "ctx" in out
        assert "do it" in out

    def test_agent_section_template_formats(self):
        out = R.AGENT_SECTION_TEMPLATE.format(agent_status="Agent status: idle")
        assert "Agent status: idle" in out

    def test_backward_compat_aliases(self):
        assert R.SUBAGENT_SECTION_TEMPLATE is R.AGENT_SECTION_TEMPLATE
        assert R.SUBAGENT_TASK_PROMPT is R.AGENT_TASK_PROMPT


class TestMiscConstants:
    def test_summarize_duplicate_has_language_field(self):
        out = R.SUMMARIZE_PROBLEM_WHEN_DUPLICATE.format(language="Chinese")
        assert "Chinese" in out

    def test_json_repair_prompt_fields(self):
        out = R.JSON_REPAIR_PROMPT.format(json_data="{}", json_decode_error="boom")
        assert "boom" in out

    def test_cmd_prompt_is_empty_base(self):
        # The base command template is empty: the old "# Current State" block
        # (live cwd + wall-clock time) moved to per-turn reminder sources. The
        # trailing user prompt is assembled from memory_context + reminders.
        assert R.CMD_PROMPT == ""
