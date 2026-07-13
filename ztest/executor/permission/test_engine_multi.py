#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for ``PermissionEngine.check_multi`` (multi-path folding).

A patch that touches several paths in one call is evaluated path-by-path and
folded strictest-wins: any deny -> deny; else any ask/sandbox-escalation -> one
consolidated prompt; else allow. ``check()`` (single-target) must be unchanged.
"""
from __future__ import annotations

import os

import pytest

from mote.common.schema import PermissionConfig, SandboxConfig
from mote.executor.permission.engine import PermissionEngine
from mote.executor.permission.rule_store import RuleStore
from mote.executor.permission.sandbox import SandboxGuard

pytestmark = pytest.mark.asyncio


# The engine's ``ask_user`` now takes a structured ``ApprovalRequest`` and returns
# an ``ApprovalChoice``. ``_test_prompts`` collects the requests (tests inspect
# ``request.target`` for the folded path list); replies stay in the readable
# yes/always/no vocabulary and map to the choice the engine consumes.
_REPLY_TO_CHOICE = {
    "yes": "allow_once",
    "always": "allow_session",
    "no": "deny",
}


def engine(mode="default", *, allow=None, deny=None, ask=None, reply=None):
    cfg = PermissionConfig(mode=mode, allow=allow or [], deny=deny or [], ask=ask or [])
    store = RuleStore.from_config(cfg)
    ask_user = None
    prompts: list = []
    if reply is not None:
        choice = _REPLY_TO_CHOICE.get(reply, reply)

        async def ask_user(request) -> str:  # noqa: E306
            prompts.append(request)
            return choice

    eng = PermissionEngine(mode=mode, store=store, ask_user=ask_user)
    eng._test_prompts = prompts  # type: ignore[attr-defined]
    return eng


def sandboxed_engine(cwd, *, mode="bypass", reply=None):
    cfg = PermissionConfig(mode=mode)
    store = RuleStore.from_config(cfg)
    ask_user = None
    prompts: list = []
    if reply is not None:
        choice = _REPLY_TO_CHOICE.get(reply, reply)

        async def ask_user(request) -> str:  # noqa: E306
            prompts.append(request)
            return choice

    guard = SandboxGuard(
        SandboxConfig(mode="workspace-write", writable_roots=[]),
        get_cwd=lambda: cwd,
    )
    eng = PermissionEngine(mode=mode, store=store, ask_user=ask_user, sandbox=guard)
    eng._test_prompts = prompts  # type: ignore[attr-defined]
    return eng


class TestFolding:
    async def test_all_allow(self):
        d = await engine("bypass").check_multi("ApplyPatch", targets=["/a.py", "/b.py"])
        assert d.behavior == "allow"

    async def test_deny_wins(self):
        # One path is denied by rule -> the whole call is denied immediately.
        d = await engine("bypass", deny=["ApplyPatch(/secret*)"]).check_multi(
            "ApplyPatch", targets=["/a.py", "/secret.py"]
        )
        assert d.behavior == "deny"

    async def test_single_consolidated_ask_for_multiple_paths(self):
        eng = engine("default", reply="yes")  # default => ask each path
        d = await eng.check_multi("ApplyPatch", targets=["/a.py", "/b.py", "/c.py"])
        assert d.behavior == "allow"
        # Exactly ONE prompt covering all asking paths.
        assert len(eng._test_prompts) == 1
        target = eng._test_prompts[0].target
        assert "/a.py" in target and "/b.py" in target and "/c.py" in target

    async def test_consolidated_ask_denied(self):
        eng = engine("default", reply="no")
        d = await eng.check_multi("ApplyPatch", targets=["/a.py", "/b.py"])
        assert d.behavior == "deny"

    async def test_no_channel_fails_closed(self):
        # default mode, no ask_user => asks become a deny.
        d = await engine("default").check_multi("ApplyPatch", targets=["/a.py", "/b.py"])
        assert d.behavior == "deny"

    async def test_always_remembers_session_rule(self):
        eng = engine("default", reply="always")
        targets = ["/a.py", "/b.py"]
        d = await eng.check_multi("ApplyPatch", targets=targets)
        assert d.behavior == "allow"
        # A subsequent identical call needs no prompt (session rules remembered).
        eng._test_prompts.clear()
        d2 = await eng.check_multi("ApplyPatch", targets=targets)
        assert d2.behavior == "allow"
        assert eng._test_prompts == []


class TestSandbox:
    async def test_escalation_outside_sandbox(self, tmp_path):
        cwd = str(tmp_path)
        outside = os.path.join(str(tmp_path.parent), "outside.py")
        inside = os.path.join(cwd, "inside.py")
        eng = sandboxed_engine(cwd, mode="bypass", reply="yes")
        d = await eng.check_multi("ApplyPatch", targets=[inside, outside], mutates_fs=True)
        assert d.behavior == "allow"
        # The escalation prompt was raised for the out-of-sandbox path.
        assert len(eng._test_prompts) == 1
        assert "outside.py" in eng._test_prompts[0].target

    async def test_escalation_blocked_no_channel(self, tmp_path):
        cwd = str(tmp_path)
        outside = os.path.join(str(tmp_path.parent), "outside.py")
        eng = sandboxed_engine(cwd, mode="bypass", reply=None)
        d = await eng.check_multi("ApplyPatch", targets=[outside], mutates_fs=True)
        assert d.behavior == "deny"


class TestSingleTargetRegression:
    async def test_empty_targets_delegates_to_check(self):
        d = await engine("bypass").check_multi("ApplyPatch", targets=[])
        assert d.behavior == "allow"

    async def test_check_still_single(self):
        # check() unchanged: bypass allows.
        d = await engine("bypass").check("Read", target="/a.py")
        assert d.behavior == "allow"
