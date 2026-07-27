#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for ``mote.runtime.tools.permission.rule_matcher``."""
from __future__ import annotations

from mote.runtime.tools.permission.rule_matcher import parse_rule, rule_matches, suggest_command_rule


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


class TestPrefixRule:
    def test_prefix_matches_variations(self):
        # A ":*" prefix rule matches any command whose token prefix starts with
        # the base — so an approved "git commit" sticks across variations.
        r = parse_rule("Bash(git commit:*)", "allow")
        assert rule_matches(r, "Bash", 'git commit -m "fix typo"')
        assert rule_matches(r, "Bash", "git commit -a")
        assert rule_matches(r, "Bash", "git commit")

    def test_prefix_respects_token_boundary(self):
        # "git commit" must not match "git commit-tree" (whole-token prefix).
        r = parse_rule("Bash(git commit:*)", "allow")
        assert not rule_matches(r, "Bash", "git commit-tree HEAD")

    def test_prefix_wrong_command_no_match(self):
        r = parse_rule("Bash(git commit:*)", "allow")
        assert not rule_matches(r, "Bash", "git status")

    def test_prefix_single_token(self):
        r = parse_rule("Bash(ls:*)", "allow")
        assert rule_matches(r, "Bash", "ls -la")
        assert not rule_matches(r, "Bash", "lsof")

    def test_prefix_env_stripped_target(self):
        # The target's leading safe env-var is stripped before matching.
        r = parse_rule("Bash(npm run:*)", "allow")
        assert rule_matches(r, "Bash", "NODE_ENV=prod npm run build")

    def test_prefix_unsafe_env_target_no_match(self):
        # An unsafe env assignment makes the target unparseable -> no match.
        r = parse_rule("Bash(ls:*)", "allow")
        assert not rule_matches(r, "Bash", "PATH=/evil ls")

    def test_prefix_unparseable_target_no_match(self):
        r = parse_rule("Bash(echo:*)", "allow")
        assert not rule_matches(r, "Bash", 'echo "unterminated')


class TestSuggestCommandRule:
    def test_subcommand_folds_to_prefix(self):
        rule = suggest_command_rule("Bash", 'git commit -m "x"')
        assert rule is not None
        assert rule.tool_name == "Bash"
        assert rule.pattern == "git commit:*"
        assert rule.behavior == "allow"
        assert rule.source == "session"

    def test_plain_command_prefix(self):
        rule = suggest_command_rule("Bash", "ls -la")
        assert rule is not None
        assert rule.pattern == "ls:*"

    def test_unsafe_env_returns_none(self):
        assert suggest_command_rule("Bash", "PATH=/evil ls") is None

    def test_unparseable_returns_none(self):
        assert suggest_command_rule("Bash", 'echo "x') is None

    def test_suggested_rule_matches_origin(self):
        # The rule an "always" grant creates must match the command that created
        # it — and its variations.
        rule = suggest_command_rule("Bash", "git commit -m first")
        assert rule is not None
        assert rule_matches(rule, "Bash", "git commit -m first")
        assert rule_matches(rule, "Bash", "git commit -m second")
