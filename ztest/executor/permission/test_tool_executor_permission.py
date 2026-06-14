#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""End-to-end: the permission gate inside ``ToolExecutor.run_command``.

Confirms (a) with no PermissionConfig the executor behaves exactly as before
(no approval layer), and (b) with a config, denied calls never reach the tool
and the user's approval is routed through the Role's ``request_approval``
capability.
"""
from __future__ import annotations

from typing import Any

import pytest

from metagpt.common.schema import PermissionConfig
from metagpt.executor.base_tool import BaseTool
from metagpt.executor.permission.types import PermissionDecision
from metagpt.executor.tool_executor import ToolExecutor

pytestmark = pytest.mark.asyncio


class SpyTool(BaseTool):
    """Records whether it actually executed, and echoes its arg."""

    name = "Spy"

    def __init__(self) -> None:
        super().__init__()
        self.ran = False

    def permission_target(self, args: dict) -> str:
        return args.get("cmd") or ""

    async def call(self, *, cmd: str = "") -> str:
        self.ran = True
        return f"ran:{cmd}"


class SafetyTool(BaseTool):
    """Always forces an ask via its self-check."""

    name = "Danger"

    def check_permissions(self, args: dict) -> "PermissionDecision | None":
        return PermissionDecision.ask("tool_check", "always confirm")

    async def call(self) -> str:
        return "done"


class FakeRole:
    """Publishes a request_approval capability returning a canned reply."""

    def __init__(self, reply: str = "no") -> None:
        self.reply = reply
        self.asked: list[str] = []

    def tool_capabilities(self) -> dict[str, Any]:
        return {"request_approval": self._approve}

    async def _approve(self, prompt: str) -> str:
        self.asked.append(prompt)
        return self.reply


def build(tool: BaseTool, *, config: PermissionConfig | None, role: FakeRole | None = None) -> ToolExecutor:
    ex = ToolExecutor("sess", tools=None, role=role, permission_config=config)
    tool.bind("sess")
    ex.register_tool_instance(tool, [tool.name, *getattr(tool, "aliases", [])])
    return ex


class TestNoConfigIsLegacy:
    async def test_runs_without_approval_layer(self):
        tool = SpyTool()
        ex = build(tool, config=None)
        res = await ex.run_command("Spy", {"cmd": "ls"})
        assert res.success and tool.ran
        assert res.output == "ran:ls"


class TestDenyRule:
    async def test_denied_call_never_reaches_tool(self):
        tool = SpyTool()
        ex = build(tool, config=PermissionConfig(deny=["Spy"]))
        res = await ex.run_command("Spy", {"cmd": "ls"})
        assert res.success is False
        assert tool.ran is False
        assert "PERMISSION DENIED" in res.output


class TestAllowRule:
    async def test_allowed_call_runs(self):
        tool = SpyTool()
        ex = build(tool, config=PermissionConfig(allow=["Spy"]))
        res = await ex.run_command("Spy", {"cmd": "ls"})
        assert res.success and tool.ran


class TestInteractiveApproval:
    async def test_ask_routed_to_role_and_denied(self):
        tool = SafetyTool()
        role = FakeRole(reply="no")
        ex = build(tool, config=PermissionConfig(mode="default"), role=role)
        res = await ex.run_command("Danger", {})
        assert res.success is False
        assert role.asked, "request_approval should have been called"

    async def test_ask_routed_to_role_and_approved(self):
        tool = SafetyTool()
        role = FakeRole(reply="yes")
        ex = build(tool, config=PermissionConfig(mode="default"), role=role)
        res = await ex.run_command("Danger", {})
        assert res.success and res.output == "done"
