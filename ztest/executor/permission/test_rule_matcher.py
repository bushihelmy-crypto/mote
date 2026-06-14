#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for ``metagpt.executor.permission.rule_matcher``."""
from __future__ import annotations

from metagpt.executor.permission.rule_matcher import parse_rule, rule_matches


class TestParseRule:
    def test_whole_tool_rule(self):
        r = parse_rule("Read", "allow")
        assert r.tool_name == "Read"
        assert r.pattern is None
        assert r.behavior == "allow"

    def test_pattern_rule(self):
        r = parse_rule("Bash(git commit)", "allow")
        assert r.tool_name == "Bash"
        assert r.pattern == "git commit"

    def test_glob_pattern(self):
        r = parse_rule("Bash(npm install*)", "deny")
        assert r.tool_name == "Bash"
        assert r.pattern == "npm install*"

    def test_empty_pattern_is_whole_tool(self):
        assert parse_rule("Bash()", "allow").pattern is None

    def test_nested_parens_preserved(self):
        r = parse_rule("Bash(echo (hi))", "allow")
        assert r.pattern == "echo (hi)"

    def test_source_recorded(self):
        assert parse_rule("Read", "allow", source="role").source == "role"


class TestRuleMatches:
    def test_whole_tool_matches_any_target(self):
        r = parse_rule("Read", "allow")
        assert rule_matches(r, "Read", "anything")
        assert rule_matches(r, "Read", "")

    def test_wrong_tool_name(self):
        r = parse_rule("Read", "allow")
        assert not rule_matches(r, "Write", "x")

    def test_pattern_glob_match(self):
        r = parse_rule("Bash(git*)", "allow")
        assert rule_matches(r, "Bash", "git status")
        assert not rule_matches(r, "Bash", "rm -rf /")

    def test_exact_pattern(self):
        r = parse_rule("Bash(git commit)", "allow")
        assert rule_matches(r, "Bash", "git commit")
        assert not rule_matches(r, "Bash", "git commit -a")

    def test_mcp_namespace_rule_matches_all_server_tools(self):
        r = parse_rule("mcp__github", "allow")
        assert rule_matches(r, "mcp__github__search", "")
        assert rule_matches(r, "mcp__github__create_issue", "")
        assert not rule_matches(r, "mcp__gitlab__search", "")

    def test_tool_name_glob(self):
        r = parse_rule("mcp__github__*", "allow")
        assert rule_matches(r, "mcp__github__search", "")
        assert not rule_matches(r, "mcp__gitlab__search", "")
