#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for ``mote.runtime.tools.permission.command_parse``."""
from __future__ import annotations

from mote.runtime.tools.permission.command_parse import command_prefix, parse_segments, prefix_tokens, segment_strings


class TestParseSegments:
    def test_simple_command(self):
        assert parse_segments("ls -la") == [["ls", "-la"]]

    def test_operators_split(self):
        assert parse_segments("git status && ls | grep x") == [
            ["git", "status"],
            ["ls"],
            ["grep", "x"],
        ]

    def test_semicolon_and_background(self):
        assert parse_segments("a ; b & c") == [["a"], ["b"], ["c"]]

    def test_shell_c_unwrapped(self):
        assert parse_segments('bash -c "git status && ls"') == [
            ["git", "status"],
            ["ls"],
        ]

    def test_unbalanced_quotes_returns_none(self):
        assert parse_segments('echo "unterminated') is None

    def test_empty(self):
        assert parse_segments("") == []


class TestSegmentStrings:
    def test_splits_and_requotes(self):
        assert segment_strings("git status && rm -rf /tmp") == ["git status", "rm -rf /tmp"]

    def test_unparseable_falls_back_to_whole(self):
        # Unbalanced quotes -> still hand back the original so callers evaluate
        # *something* rather than silently dropping the call.
        assert segment_strings('echo "x') == ['echo "x']

    def test_empty(self):
        assert segment_strings("   ") == []


class TestCommandPrefix:
    def test_subcommand_folded(self):
        assert command_prefix('git commit -m "fix typo"') == "git commit"
        assert command_prefix("npm install foo") == "npm install"

    def test_plain_command_no_subcommand(self):
        assert command_prefix("ls -la") == "ls"

    def test_non_subcommand_tool_stays_bare(self):
        # `cat` is not in the sub-command set; prefix is just the command.
        assert command_prefix("cat a b c") == "cat"

    def test_flag_after_subcommand_tool_not_folded(self):
        # `git -C /x status` — the token after git is a flag, not a subcommand.
        assert command_prefix("git -C /other status") == "git"

    def test_safe_env_skipped(self):
        assert command_prefix("NODE_ENV=prod npm run build") == "npm run"

    def test_unsafe_env_blocks_prefix(self):
        assert command_prefix("PATH=/evil ls") is None
        assert command_prefix("LD_PRELOAD=x.so cat /etc/passwd") is None

    def test_absolute_binary_basename(self):
        assert command_prefix("/usr/bin/git status") == "git status"

    def test_unparseable_returns_none(self):
        assert command_prefix('echo "x') is None

    def test_numeric_token_not_subcommand(self):
        assert command_prefix("go 123") == "go"


class TestPrefixTokens:
    def test_env_stripped(self):
        assert prefix_tokens("FOO=1 git commit -m x") == ["git", "commit", "-m", "x"]

    def test_unsafe_env_none(self):
        assert prefix_tokens("PATH=/x ls") is None

    def test_unparseable_none(self):
        assert prefix_tokens('echo "x') is None
