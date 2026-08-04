#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""End-to-end: the permission gate inside ``ToolExecutor.run_command``.

Confirms (a) with no PermissionConfig the executor behaves exactly as before
(no approval layer), and (b) with a config, denied calls never reach the tool
and the user's approval is routed through the Role's ``request_approval``
capability.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from mote.contracts.authorization import PermissionDecision
from mote.contracts.tool import ToolAttemptOrdinal, ToolInvocationId, ToolInvocationIdentity, tool_arguments_digest
from mote.product.toolsets.builtin.edit import Edit
from mote.product.toolsets.builtin.read import Read
from mote.product.toolsets.builtin.search import Search
from mote.runtime.session.workspace import SessionWorkspace
from mote.runtime.tools.base_tool import BaseTool
from mote.runtime.tools.definitions import native_definition
from mote.runtime.tools.permission import PermissionEngine, RuleStore
from mote.runtime.tools.permission.config import PermissionConfig, SandboxConfig
from mote.runtime.tools.permission.sandbox.guard import SandboxGuard
from mote.runtime.tools.policy import DefaultToolCallPolicy, build_tool_call_policy
from mote.runtime.tools.tool_executor import ToolExecutor
from mote.runtime.tools.tool_pipeline import AuthorizeStage, ToolExecution

pytestmark = pytest.mark.asyncio


def invocation_identity(call_id: str, arguments: dict[str, object]) -> ToolInvocationIdentity:
    return ToolInvocationIdentity(
        invocation_id=ToolInvocationId(call_id),
        attempt_ordinal=ToolAttemptOrdinal(1),
        definition_identity="mote.test.tool/v1",
        catalog_generation=1,
        arguments_digest=tool_arguments_digest(arguments),
        owner_id="test-agent",
        run_id="test-run",
    )


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


class EmptyTargetMutatingTool(BaseTool):
    """Filesystem-mutating tool that cannot identify the path it would touch."""

    name = "EmptyTargetMutating"
    mutates_filesystem = True

    async def call(self) -> str:
        return "must not run"


class FakeRole:
    """Publishes a request_approval capability returning a canned ApprovalChoice.

    The capability now receives a structured ``ApprovalRequest`` and returns an
    ``ApprovalChoice``; tests still express intent in the readable yes/no
    vocabulary, mapped here to the choice the engine consumes.
    """

    _REPLY_TO_CHOICE = {"yes": "allow_once", "always": "allow_session", "no": "deny"}

    def __init__(self, reply: str = "no") -> None:
        self.reply = self._REPLY_TO_CHOICE.get(reply, reply)
        self.asked: list = []

    def tool_capabilities(self) -> dict[str, Any]:
        return {"request_approval": self._approve}

    async def _approve(self, request: Any) -> str:
        self.asked.append(request)
        return self.reply


def build(tool: BaseTool, *, config: PermissionConfig | None, role: FakeRole | None = None) -> ToolExecutor:
    policy = build_tool_call_policy(config, role=role)
    workspace_tmp = tempfile.TemporaryDirectory()
    ex = ToolExecutor(
        "sess",
        tools=None,
        role=role,
        tool_call_policy=policy,
        workspace_store=SessionWorkspace(Path(workspace_tmp.name)),
    )
    ex._test_workspace_tmp = workspace_tmp
    ex.register_native_tool(native_definition(type(tool)), tool)
    return ex


def authorize_stage(config: PermissionConfig, *, cwd: str | None = None) -> AuthorizeStage:
    sandbox = None
    if config.sandbox is not None:
        sandbox = SandboxGuard(config.sandbox, get_cwd=lambda: cwd or "")
    engine = PermissionEngine(
        mode=config.mode,
        store=RuleStore.from_config(config),
        sandbox=sandbox,
    )
    return AuthorizeStage(DefaultToolCallPolicy(permission_engine=engine))


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
        assert 'code="TOOL_PERMISSION_DENIED"' in res.output
        assert res.error is not None and res.error.code == "TOOL_PERMISSION_DENIED"

    async def test_rule_deny_is_recoverable_not_terminal(self):
        # A rule/mode deny fails the call but must NOT end the react loop — the
        # model can replan around it. So ``terminate`` stays False.
        tool = SpyTool()
        ex = build(tool, config=PermissionConfig(deny=["Spy"]))
        res = await ex.run_command("Spy", {"cmd": "ls"})
        assert res.success is False
        assert res.terminate is False


class TestAllowRule:
    async def test_allowed_call_runs(self):
        tool = SpyTool()
        ex = build(tool, config=PermissionConfig(allow=["Spy"]))
        res = await ex.run_command("Spy", {"cmd": "ls"})
        assert res.success and tool.ran


class TestFilesystemPermissionTargets:
    @pytest.mark.parametrize(
        ("tool_type", "args", "relative_path"),
        [
            pytest.param(Read, {"file_path": "pkg/../pkg/a.py"}, "pkg/a.py", id="read"),
            pytest.param(Edit, {"file_path": "./pkg/a.py"}, "pkg/a.py", id="edit"),
            pytest.param(Search, {"path": "pkg/./sub/.."}, "pkg", id="search"),
        ],
    )
    async def test_relative_path_matches_canonical_absolute_rule(
        self,
        tmp_path,
        tool_type,
        args,
        relative_path,
    ):
        tool = tool_type()
        tool.get_cwd = lambda: str(tmp_path)
        expected = os.path.realpath(tmp_path / relative_path)
        stage = authorize_stage(
            PermissionConfig(
                mode="dontAsk",
                allow=[f"{tool.name}({expected})"],
            )
        )
        execution = ToolExecution(
            name=tool.name,
            args=args,
            identity=invocation_identity("canonical-path-call", args),
            tool=tool,
        )

        assert tool.permission_target(args) == expected
        assert await stage.run(execution) is None

    async def test_empty_mutating_target_is_denied_by_authorize_stage(self, tmp_path):
        tool = EmptyTargetMutatingTool()
        stage = authorize_stage(
            PermissionConfig(
                mode="bypass",
                sandbox=SandboxConfig(),
            ),
            cwd=str(tmp_path),
        )
        execution = ToolExecution(
            name=tool.name,
            args={},
            identity=invocation_identity("empty-target-call", {}),
            tool=tool,
        )

        assert tool.permission_targets({}) == []
        denied = await stage.run(execution)
        assert denied is not None
        assert denied.success is False
        assert denied.error is not None
        assert denied.error.code == "TOOL_PERMISSION_DENIED"
        assert "no concrete permission target" in denied.output


class TestInteractiveApproval:
    async def test_ask_routed_to_role_and_denied(self):
        tool = SafetyTool()
        role = FakeRole(reply="no")
        ex = build(tool, config=PermissionConfig(mode="default"), role=role)
        res = await ex.run_command("Danger", {})
        assert res.success is False
        assert role.asked, "request_approval should have been called"

    async def test_user_rejection_marks_result_terminal(self):
        # A genuine user "no" at the approval prompt ends the react loop: the
        # failed result carries ``terminate`` so the loop clears the active signal.
        tool = SafetyTool()
        role = FakeRole(reply="no")
        ex = build(tool, config=PermissionConfig(mode="default"), role=role)
        res = await ex.run_command("Danger", {})
        assert res.success is False
        assert res.terminate is True

    async def test_ask_routed_to_role_and_approved(self):
        tool = SafetyTool()
        role = FakeRole(reply="yes")
        ex = build(tool, config=PermissionConfig(mode="default"), role=role)
        res = await ex.run_command("Danger", {})
        assert res.success and res.output == "done"
