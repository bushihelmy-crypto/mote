#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Dispatch-level tests: ``ToolExecutor`` routes ``ApplyPatch`` through the
multi-path permission path and never writes a denied patch.

A patch touching >1 path must be evaluated via ``PermissionEngine.check_multi``
(one consolidated decision); a single-path patch keeps the ``check`` path. A
denied multi-path patch must abort before any file is written.
"""
from __future__ import annotations

import os

import pytest

from metagpt.common.schema import PermissionConfig
from metagpt.executor.tool_executor import ToolExecutor
from metagpt.executor.tools.apply_patch import ApplyPatch

pytestmark = pytest.mark.asyncio


def _wrap(body: str) -> str:
    return "*** Begin Patch\n" + body + "\n*** End Patch"


def build(*, config: PermissionConfig) -> tuple[ToolExecutor, ApplyPatch]:
    tool = ApplyPatch()
    ex = ToolExecutor("sess", tools=None, role=None, permission_config=config)
    tool.bind("sess")
    ex.register_tool_instance(tool, [tool.name, *tool.aliases])
    return ex, tool


class TestDispatchRouting:
    async def test_multi_path_uses_check_multi(self, workspace, monkeypatch):
        ex, _ = build(config=PermissionConfig(mode="bypass"))
        calls = {"multi": 0, "single": 0}
        engine = ex._permission_engine
        orig_multi = engine.check_multi
        orig_single = engine.check

        async def spy_multi(*a, **k):
            calls["multi"] += 1
            return await orig_multi(*a, **k)

        async def spy_single(*a, **k):
            calls["single"] += 1
            return await orig_single(*a, **k)

        monkeypatch.setattr(engine, "check_multi", spy_multi)
        monkeypatch.setattr(engine, "check", spy_single)

        patch = _wrap("*** Add File: a.py\n+x\n*** Add File: b.py\n+y")
        res = await ex.run_command("ApplyPatch", {"input": patch})
        assert res.success, res.output
        assert calls["multi"] == 1
        assert calls["single"] == 0
        assert os.path.exists(os.path.abspath("a.py"))
        assert os.path.exists(os.path.abspath("b.py"))

    async def test_single_path_uses_check(self, workspace, monkeypatch):
        ex, _ = build(config=PermissionConfig(mode="bypass"))
        calls = {"multi": 0, "single": 0}
        engine = ex._permission_engine
        orig_multi = engine.check_multi
        orig_single = engine.check

        async def spy_multi(*a, **k):
            calls["multi"] += 1
            return await orig_multi(*a, **k)

        async def spy_single(*a, **k):
            calls["single"] += 1
            return await orig_single(*a, **k)

        monkeypatch.setattr(engine, "check_multi", spy_multi)
        monkeypatch.setattr(engine, "check", spy_single)

        patch = _wrap("*** Add File: only.py\n+x")
        res = await ex.run_command("ApplyPatch", {"input": patch})
        assert res.success, res.output
        assert calls["single"] == 1
        assert calls["multi"] == 0


class TestDeniedNeverWrites:
    async def test_denied_multi_patch_writes_nothing(self, workspace):
        # Deny one of the two affected paths -> strictest-wins deny, no writes.
        secret = os.path.abspath("secret.py")
        ex, _ = build(config=PermissionConfig(mode="bypass", deny=[f"ApplyPatch({secret})"]))
        patch = _wrap("*** Add File: ok.py\n+x\n*** Add File: secret.py\n+y")
        res = await ex.run_command("ApplyPatch", {"input": patch})
        assert res.success is False
        assert "PERMISSION DENIED" in res.output
        assert not os.path.exists(os.path.abspath("ok.py"))
        assert not os.path.exists(secret)
