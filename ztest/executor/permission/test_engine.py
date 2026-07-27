#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for ``mote.runtime.tools.permission.engine.PermissionEngine``.

Covers the 11-step decision pipeline: bypass-immune deny/ask, mode shortcuts
(bypass / acceptEdits / plan / dontAsk), rule allows, tool self-checks, and the
interactive ``ask`` resolution (yes / always / no, plus the no-channel
fail-closed path).
"""
from __future__ import annotations

import os

import pytest

from mote.contracts.permissions import PermissionDecision
from mote.contracts.settings.permissions import PermissionConfig, SandboxConfig
from mote.runtime.tools.permission.engine import PermissionEngine
from mote.runtime.tools.permission.rule_store import RuleStore
from mote.runtime.tools.permission.sandbox.guard import SandboxGuard

pytestmark = pytest.mark.asyncio


# The engine's ``ask_user`` now returns a structured ``ApprovalChoice`` (the
# display layer owns all human wording). Tests still express intent in the old
# yes/always/no vocabulary for readability; this maps it to the choice the
# engine consumes.
_REPLY_TO_CHOICE = {
    "yes": "allow_once",
    "always": "allow_session",
    "no": "deny",
}


def engine(mode="default", *, allow=None, deny=None, ask=None, reply=None):
    """Build an engine with a canned (optional) approval reply."""
    cfg = PermissionConfig(mode=mode, allow=allow or [], deny=deny or [], ask=ask or [])
    store = RuleStore.from_config(cfg)
    ask_user = None
    if reply is not None:
        choice = _REPLY_TO_CHOICE.get(reply, reply)

        async def ask_user(_request) -> str:  # noqa: E306
            return choice

    return PermissionEngine(mode=mode, store=store, ask_user=ask_user)


def sandboxed_engine(cwd, *, mode="bypass", reply=None, writable_roots=None):
    """Build an engine whose allows are gated by a workspace-write sandbox."""
    cfg = PermissionConfig(mode=mode)
    store = RuleStore.from_config(cfg)
    ask_user = None
    if reply is not None:
        choice = _REPLY_TO_CHOICE.get(reply, reply)

        async def ask_user(_request) -> str:  # noqa: E306
            return choice

    guard = SandboxGuard(
        SandboxConfig(mode="workspace-write", writable_roots=writable_roots or []),
        get_cwd=lambda: cwd,
    )
    return PermissionEngine(mode=mode, store=store, ask_user=ask_user, sandbox=guard)


class TestRules:
    async def test_deny_rule_blocks(self):
        d = await engine(deny=["Bash(rm -rf*)"]).check("Bash", target="rm -rf /")
        assert d.behavior == "deny"

    async def test_allow_rule_passes(self):
        d = await engine(allow=["Read"]).check("Read", target="")
        assert d.behavior == "allow"

    async def test_deny_immune_to_bypass(self):
        d = await engine("bypass", deny=["Bash(rm -rf*)"]).check("Bash", target="rm -rf /")
        assert d.behavior == "deny"

    async def test_ask_rule_immune_to_bypass(self):
        # Even in bypass, an ask rule prompts; "no" => deny.
        d = await engine("bypass", ask=["Write"], reply="no").check("Write", target="/x")
        assert d.behavior == "deny"


class TestModes:
    async def test_bypass_allows_unmatched(self):
        d = await engine("bypass").check("Bash", target="ls")
        assert d.behavior == "allow"

    async def test_accept_edits_allows_mutating(self):
        d = await engine("acceptEdits").check("Write", target="/x", mutates_fs=True)
        assert d.behavior == "allow"

    async def test_accept_edits_does_not_allow_nonmutating(self):
        # Non-mutating tool in acceptEdits falls through to ask; no channel => deny.
        d = await engine("acceptEdits").check("Bash", target="ls", mutates_fs=False)
        assert d.behavior == "deny"

    async def test_plan_denies(self):
        d = await engine("plan").check("Write", target="/x", mutates_fs=True)
        assert d.behavior == "deny"
        assert "plan mode" in d.reason.detail

    async def test_dont_ask_denies(self):
        d = await engine("dontAsk").check("Bash", target="ls")
        assert d.behavior == "deny"


class TestToolCheck:
    async def test_tool_check_deny_immune_to_bypass(self):
        tc = PermissionDecision.deny("tool_check", "dangerous")
        d = await engine("bypass").check("Bash", target="x", tool_check=tc)
        assert d.behavior == "deny"

    async def test_tool_check_ask_prompts(self):
        tc = PermissionDecision.ask("tool_check", "confirm")
        d = await engine(reply="yes").check("Bash", target="x", tool_check=tc)
        assert d.behavior == "allow"

    async def test_tool_check_allow(self):
        tc = PermissionDecision.allow("tool_check", "safe")
        d = await engine("dontAsk").check("Bash", target="x", tool_check=tc)
        assert d.behavior == "allow"


class TestAskResolution:
    async def test_yes_allows_once(self):
        d = await engine(reply="yes").check("Bash", target="ls")
        assert d.behavior == "allow"

    async def test_no_denies(self):
        d = await engine(reply="no").check("Bash", target="ls")
        assert d.behavior == "deny"

    async def test_always_allows_and_remembers(self):
        eng = engine(reply="always")
        d1 = await eng.check("Bash", target="make test")
        assert d1.behavior == "allow"
        # Second call hits the session rule — even with a deny-everything reply
        # it stays allowed because the rule short-circuits before prompting.
        eng._ask_user = None  # ensure no prompt; rule must carry it
        d2 = await eng.check("Bash", target="make test")
        assert d2.behavior == "allow"

    async def test_no_channel_fails_closed(self):
        d = await engine(reply=None).check("Bash", target="ls")
        assert d.behavior == "deny"
        assert "no interactive channel" in d.message


class TestSegments:
    async def test_compound_deny_catches_dangerous_half(self):
        # A deny rule on the destructive segment blocks the whole command, even
        # though the leading segment is harmless.
        eng = engine(deny=["Bash(rm -rf*)"])
        d = await eng.check("Bash", target="ls && rm -rf /", segments=["ls", "rm -rf /"])
        assert d.behavior == "deny"

    async def test_compound_all_allow(self):
        eng = engine(allow=["Bash(git*)"])
        d = await eng.check(
            "Bash",
            target="git status && git log",
            segments=["git status", "git log"],
        )
        assert d.behavior == "allow"

    async def test_compound_partial_allow_defers_to_ask(self):
        # One segment allowed, the other unmatched -> defers to ask; no channel
        # => deny.
        eng = engine(allow=["Bash(git*)"], reply=None)
        d = await eng.check("Bash", target="git status && ls", segments=["git status", "ls"])
        assert d.behavior == "deny"

    async def test_ask_segment_prompts(self):
        eng = engine(ask=["Bash(deploy*)"], allow=["Bash(git*)"], reply="yes")
        d = await eng.check(
            "Bash",
            target="git status && deploy prod",
            segments=["git status", "deploy prod"],
        )
        assert d.behavior == "allow"


class TestStickyPrefix:
    async def test_always_remembers_prefix_for_single_segment(self):
        eng = engine(reply="always")
        d1 = await eng.check("Bash", target="git commit -m foo", segments=["git commit -m foo"])
        assert d1.behavior == "allow"
        # A variation of the same command rides the prefix rule without asking.
        eng._ask_user = None
        d2 = await eng.check("Bash", target="git commit -m bar", segments=["git commit -m bar"])
        assert d2.behavior == "allow"

    async def test_prefix_does_not_overgrant_other_subcommand(self):
        eng = engine(reply="always")
        d1 = await eng.check("Bash", target="git commit -m foo", segments=["git commit -m foo"])
        assert d1.behavior == "allow"
        # A different git subcommand is NOT covered by "git commit:*" — still asks
        # (no channel => deny).
        eng._ask_user = None
        d2 = await eng.check("Bash", target="git push origin main", segments=["git push origin main"])
        assert d2.behavior == "deny"

    async def test_compound_command_uses_exact_rule_not_prefix(self):
        # A multi-segment command falls back to an exact-target rule; an exact
        # variation does not re-match.
        eng = engine(reply="always")
        d1 = await eng.check(
            "Bash",
            target="git status && git log",
            segments=["git status", "git log"],
        )
        assert d1.behavior == "allow"
        eng._ask_user = None
        d2 = await eng.check(
            "Bash",
            target="git status && git diff",
            segments=["git status", "git diff"],
        )
        assert d2.behavior == "deny"

    async def test_prompt_shows_suggested_prefix_rule(self):
        seen = {}

        async def ask_user(request) -> str:
            seen["request"] = request
            return "deny"

        cfg = PermissionConfig(mode="default")
        store = RuleStore.from_config(cfg)
        eng = PermissionEngine(mode="default", store=store, ask_user=ask_user)
        await eng.check("Bash", target="git commit -m foo", segments=["git commit -m foo"])
        assert seen["request"].suggestion == "Bash(git commit:*)"


class TestSandbox:
    async def test_empty_mutating_target_fails_closed(self, tmp_path):
        eng = sandboxed_engine(str(tmp_path))
        d = await eng.check("Edit", target="", mutates_fs=True)
        assert d.behavior == "deny"
        assert d.reason.type == "sandbox"
        assert "no concrete permission target" in d.message

    async def test_write_inside_sandbox_allowed(self, tmp_path):
        cwd = str(tmp_path)
        eng = sandboxed_engine(cwd)
        d = await eng.check("Write", target=f"{cwd}/f.txt", mutates_fs=True)
        assert d.behavior == "allow"

    async def test_write_outside_escalates_and_denied(self, tmp_path):
        cwd = str(tmp_path / "ws")
        os.makedirs(cwd, exist_ok=True)
        eng = sandboxed_engine(cwd, reply="no")
        d = await eng.check("Write", target=str(tmp_path / "outside.txt"), mutates_fs=True)
        assert d.behavior == "deny"
        assert d.reason.type == "sandbox"

    async def test_write_outside_escalation_approved_once(self, tmp_path):
        cwd = str(tmp_path / "ws")
        os.makedirs(cwd, exist_ok=True)
        eng = sandboxed_engine(cwd, reply="yes")
        d = await eng.check("Write", target=str(tmp_path / "outside.txt"), mutates_fs=True)
        assert d.behavior == "allow"
        assert d.reason.type == "user"

    async def test_write_outside_escalation_always_widens_sandbox(self, tmp_path):
        cwd = str(tmp_path / "ws")
        os.makedirs(cwd, exist_ok=True)
        granted_dir = tmp_path / "granted"
        granted_dir.mkdir()
        eng = sandboxed_engine(cwd, reply="always")
        first = str(granted_dir / "a.txt")
        d1 = await eng.check("Write", target=first, mutates_fs=True)
        assert d1.behavior == "allow"
        # A sibling write under the now-granted directory passes without prompting.
        eng._ask_user = None
        d2 = await eng.check("Write", target=str(granted_dir / "b.txt"), mutates_fs=True)
        assert d2.behavior == "allow"

    async def test_no_channel_escalation_fails_closed(self, tmp_path):
        cwd = str(tmp_path / "ws")
        os.makedirs(cwd, exist_ok=True)
        eng = sandboxed_engine(cwd, reply=None)
        d = await eng.check("Write", target=str(tmp_path / "outside.txt"), mutates_fs=True)
        assert d.behavior == "deny"
        assert "no channel to escalate" in d.message

    async def test_non_mutating_skips_sandbox(self, tmp_path):
        # A non-fs-mutating allow is never gated by the sandbox boundary.
        cwd = str(tmp_path / "ws")
        os.makedirs(cwd, exist_ok=True)
        eng = sandboxed_engine(cwd)
        d = await eng.check("Read", target=str(tmp_path / "outside.txt"), mutates_fs=False)
        assert d.behavior == "allow"

    async def test_user_approved_write_skips_sandbox(self, tmp_path):
        # When the user just approved this turn (reason "user"), the write is
        # not re-questioned by the sandbox even if it sits outside the boundary.
        cwd = str(tmp_path / "ws")
        os.makedirs(cwd, exist_ok=True)
        eng = sandboxed_engine(cwd, mode="default", reply="yes")
        d = await eng.check("Write", target=str(tmp_path / "outside.txt"), mutates_fs=True)
        assert d.behavior == "allow"
        assert d.reason.type == "user"
