#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for metagpt.prompts.role — the static template/constant catalogue.

These are not behaviours but contracts: the streaming parser, PromptBuilder and
the cache-boundary split all depend on the exact placeholder names and the
position of SYSTEM_PROMPT_DYNAMIC_BOUNDARY. The tests pin those contracts so a
careless edit to a template string is caught.
"""
from __future__ import annotations

import re
from string import Template

from metagpt.common import prompt as R


class TestIdentityTemplates:
    def test_prefix_template_formats(self):
        out = Template(R.PREFIX_TEMPLATE).safe_substitute(profile="Engineer", name="Bob", goal="ship")
        assert "Engineer" in out
        assert "Bob" in out
        assert "ship" in out

    def test_constraint_template_formats(self):
        out = Template(R.CONSTRAINT_TEMPLATE).safe_substitute(constraints="be terse")
        assert "be terse" in out


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
            "${role_info}",
            "${available_commands}",
            "${mcp_tools}",
            "${domain_info}",
            "${example}",
            "${instruction}",
            "${memory}",
            "${language}",
            "${scratchpad}",
            "${env_section}",
            "${skills_info}",
            "${frc}",
            "${summarize_tool_results}",
            "${output_format}",
        ):
            assert ph in below, ph


class TestDynamicSectionPlaceholders:
    def test_language_section(self):
        assert "${language_name}" in R.LANGUAGE_SECTION

    def test_scratchpad_section(self):
        assert "${scratchpad_dir}" in R.SCRATCHPAD_SECTION

    def test_frc_section(self):
        assert "${keep_recent}" in R.FRC_SECTION

    def test_summarize_tool_results_has_no_placeholder(self):
        assert "${" not in R.SUMMARIZE_TOOL_RESULTS_SECTION


class TestDomainInfo:
    def test_mgx_info_formats_models(self):
        out = Template(R.MGX_INFO).safe_substitute(ai_capability_models="m1, m2")
        assert "m1, m2" in out

    def test_mgx_info_only_models_placeholder(self):
        # The single placeholder is ${ai_capability_models}; safe_substitute
        # leaves any literal braces (JSON/code examples) untouched.
        assert "${ai_capability_models}" in R.MGX_INFO


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
    def test_summary_prompts_nonempty(self):
        assert R.SUMMARY_PROMPT.strip()
        assert R.SUMMARY_WITH_RECOMMEND_PROMPT.strip()

    def test_recommend_prompt_mentions_tag(self):
        assert "<recommendations>" in R.SUMMARY_WITH_RECOMMEND_PROMPT

    def test_ask_human_command_shape(self):
        assert R.ASK_HUMAN_COMMAND == [{"command_name": "ask_human", "args": {"question": ""}}]

    def test_summarize_duplicate_has_language_field(self):
        out = R.SUMMARIZE_PROBLEM_WHEN_DUPLICATE.format(language="Chinese")
        assert "Chinese" in out

    def test_json_repair_prompt_fields(self):
        out = R.JSON_REPAIR_PROMPT.format(json_data="{}", json_decode_error="boom")
        assert "boom" in out

    def test_end_command_contains_end_tag(self):
        assert "<end></end>" in R.END_COMMAND

    def test_cmd_prompt_has_current_state(self):
        assert "${current_state}" in R.CMD_PROMPT
