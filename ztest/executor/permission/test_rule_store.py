#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for ``mote.executor.permission.rule_store``."""
from __future__ import annotations

from mote.common.schema import PermissionConfig
from mote.common.schema.permission_types import PermissionRule
from mote.executor.permission.rule_store import RuleStore


def make_store() -> RuleStore:
    cfg = PermissionConfig(
        allow=["Read", "Bash(git*)"],
        deny=["Bash(rm -rf*)"],
        ask=["Write"],
    )
    return RuleStore.from_config(cfg)


class TestResolve:
    def test_allow_whole_tool(self):
        assert make_store().resolve("Read", "") == "allow"

    def test_allow_pattern(self):
        assert make_store().resolve("Bash", "git status") == "allow"

    def test_no_match_returns_none(self):
        assert make_store().resolve("Bash", "ls -la") is None

    def test_ask_rule(self):
        assert make_store().resolve("Write", "/tmp/x") == "ask"

    def test_deny_beats_allow(self):
        # "Bash(rm -rf*)" deny vs "Bash(git*)" allow — different targets, but
        # deny must win when both could match a destructive command.
        store = RuleStore.from_config(PermissionConfig(allow=["Bash(rm*)"], deny=["Bash(rm -rf*)"]))
        assert store.resolve("Bash", "rm -rf /") == "deny"

    def test_ask_beats_allow(self):
        store = RuleStore.from_config(PermissionConfig(allow=["Write"], ask=["Write"]))
        assert store.resolve("Write", "/x") == "ask"


class TestSessionRules:
    def test_session_rule_added_and_resolved(self):
        store = RuleStore()
        assert store.resolve("Bash", "make test") is None
        store.add_session_rule(
            PermissionRule(tool_name="Bash", pattern="make test", behavior="allow", source="session")
        )
        assert store.resolve("Bash", "make test") == "allow"


class TestResolveSegments:
    def test_empty_segments_none(self):
        assert make_store().resolve_segments("Bash", []) is None

    def test_single_segment_delegates(self):
        assert make_store().resolve_segments("Bash", ["git status"]) == "allow"

    def test_deny_wins_over_allow(self):
        # "git status" allows, "rm -rf /" denies -> the whole command denies.
        assert make_store().resolve_segments("Bash", ["git status", "rm -rf /"]) == "deny"

    def test_ask_beats_allow(self):
        store = RuleStore.from_config(PermissionConfig(allow=["Bash(git*)"], ask=["Bash(deploy*)"]))
        assert store.resolve_segments("Bash", ["git status", "deploy now"]) == "ask"

    def test_all_allow_required(self):
        # Every segment must be allowed for the command to ride an allow rule;
        # an unmatched segment defers (None).
        assert make_store().resolve_segments("Bash", ["git status", "ls -la"]) is None

    def test_every_segment_allowed(self):
        assert make_store().resolve_segments("Bash", ["git status", "git log"]) == "allow"

    def test_deny_beats_ask(self):
        store = RuleStore.from_config(
            PermissionConfig(allow=["Bash(git*)"], ask=["Bash(deploy*)"], deny=["Bash(rm -rf*)"])
        )
        assert store.resolve_segments("Bash", ["git status", "deploy x", "rm -rf /"]) == "deny"
