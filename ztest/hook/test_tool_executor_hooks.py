#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""End-to-end: PreToolUse / PostToolUse hooks inside ToolExecutor.run_command.

Confirms the hook layer composes with the permission engine at the single
dispatch chokepoint: a PreToolUse deny blocks the tool (deny-wins), updated_args
rewrites the call, and PostToolUse additionalContext is appended to the output.
With no hook_manager the executor behaves exactly as before.
"""
from __future__ import annotations

import pytest

from metagpt.common.events import EventBus
from metagpt.common.hook.manager import HookManager
from metagpt.common.hook.subscriber import HookSubscriber
from metagpt.common.schema import PermissionConfig
from metagpt.executor.base_tool import BaseTool
from metagpt.executor.tool_executor import ToolExecutor

pytestmark = pytest.mark.asyncio


class SpyTool(BaseTool):
    name = "Spy"

    def __init__(self) -> None:
        super().__init__()
        self.ran = False
        self.seen_cmd = None

    def permission_target(self, args: dict) -> str:
        return args.get("cmd") or ""

    async def call(self, *, cmd: str = "") -> str:
        self.ran = True
        self.seen_cmd = cmd
        return f"ran:{cmd}"


def build(tool: BaseTool, *, hook_manager=None, config=None) -> ToolExecutor:
    bus = None
    if hook_manager is not None:
        bus = EventBus()
        bus.subscribe(HookSubscriber(hook_manager))
    ex = ToolExecutor("sess", tools=None, bus=bus, permission_config=config)
    tool.bind("sess")
    ex.register_tool_instance(tool, [tool.name, *getattr(tool, "aliases", [])])
    return ex


async def test_no_hook_manager_is_legacy():
    tool = SpyTool()
    ex = build(tool)
    res = await ex.run_command("Spy", {"cmd": "ls"})
    assert res.success and tool.ran and res.output == "ran:ls"


async def test_pre_tool_use_deny_blocks():
    tool = SpyTool()
    mgr = HookManager()
    mgr.register("PreToolUse", lambda hi: {"decision": "block", "reason": "no bash"})
    ex = build(tool, hook_manager=mgr)
    res = await ex.run_command("Spy", {"cmd": "ls"})
    assert res.success is False
    assert tool.ran is False
    assert 'code="TOOL_PERMISSION_DENIED"' in res.output
    assert "no bash" in res.output


async def test_pre_tool_use_updated_args_rewrites_call():
    tool = SpyTool()
    mgr = HookManager()
    mgr.register("PreToolUse", lambda hi: {"updatedInput": {"cmd": "safe"}})
    ex = build(tool, hook_manager=mgr)
    res = await ex.run_command("Spy", {"cmd": "danger"})
    assert res.success and tool.ran
    assert tool.seen_cmd == "safe"
    assert res.output == "ran:safe"


async def test_post_tool_use_appends_context():
    tool = SpyTool()
    mgr = HookManager()
    mgr.register("PostToolUse", lambda hi: {"additionalContext": "note: reviewed"})
    ex = build(tool, hook_manager=mgr)
    res = await ex.run_command("Spy", {"cmd": "ls"})
    assert res.success
    assert "ran:ls" in res.output
    assert "note: reviewed" in res.output


async def test_post_tool_use_output_rewrite_replaces_output():
    """A PostToolUse control subscriber returning ``updated_response`` replaces
    the tool's output text (truncate/redact channel), before any appended
    context."""
    from typing import Optional

    from metagpt.common.events import ToolResultOutcome
    from metagpt.common.events.types import POST_TOOL_USE, PostToolUseEvent
    from metagpt.common.interface.event_subscriber import ControlStage

    class Rewriter:
        handles = (POST_TOOL_USE,)
        stage = ControlStage.REWRITE

        async def handle_control(self, event) -> Optional[ToolResultOutcome]:
            if isinstance(event, PostToolUseEvent):
                return ToolResultOutcome(updated_response="[redacted]", additional_context=["note"])
            return None

    tool = SpyTool()
    bus = EventBus()
    bus.subscribe(Rewriter())
    ex = ToolExecutor("sess", tools=None, bus=bus)
    tool.bind("sess")
    ex.register_tool_instance(tool, [tool.name])
    res = await ex.run_command("Spy", {"cmd": "ls"})
    assert res.success and tool.ran
    # base output replaced, then context appended on top
    assert res.output == "[redacted]\nnote"


async def test_post_tool_use_block_marks_failure():
    tool = SpyTool()
    mgr = HookManager()
    mgr.register("PostToolUse", lambda hi: {"decision": "block", "reason": "bad output"})
    ex = build(tool, hook_manager=mgr)
    res = await ex.run_command("Spy", {"cmd": "ls"})
    assert res.success is False
    assert "bad output" in res.output


async def test_hook_deny_composes_with_permission_engine():
    # Permission engine would allow (allow rule), but the PreToolUse hook denies
    # -> deny wins.
    tool = SpyTool()
    mgr = HookManager()
    mgr.register("PreToolUse", lambda hi: {"decision": "block", "reason": "hook veto"})
    ex = build(tool, hook_manager=mgr, config=PermissionConfig(allow=["Spy"]))
    res = await ex.run_command("Spy", {"cmd": "ls"})
    assert res.success is False
    assert tool.ran is False
