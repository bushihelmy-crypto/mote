"""Tool hooks adapted through typed ToolCall and ToolResult policies."""

from __future__ import annotations

from typing import Any

import pytest

from mote.contracts.events.tool import ToolCallFinishedEvent
from mote.contracts.tool.errors import ToolError
from mote.runtime.hook.manager import HookManager
from mote.runtime.session.workspace import SessionWorkspace
from mote.runtime.tools.base_tool import BaseTool
from mote.runtime.tools.definitions import native_definition
from mote.runtime.tools.permission.config import PermissionConfig
from mote.runtime.tools.policy import build_tool_call_policy, build_tool_result_policy
from mote.runtime.tools.tool_executor import ToolExecutor
from mote.ztest.telemetry import InlineTelemetry

pytestmark = pytest.mark.asyncio
_workspace_store: SessionWorkspace | None = None


@pytest.fixture(autouse=True)
def _explicit_workspace(tmp_path):
    global _workspace_store
    _workspace_store = SessionWorkspace(tmp_path / "workspace")
    yield
    _workspace_store = None


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


class RaiseTool(BaseTool):
    name = "Raise"

    async def call(self) -> str:
        raise ToolError("kaboom")


class AliasSpyTool(BaseTool):
    name = "Bash"
    aliases = ["bash"]

    def __init__(self) -> None:
        super().__init__()
        self.ran = False

    async def call(self) -> str:
        self.ran = True
        return "ran"


class FinishedObserver:
    def __init__(self) -> None:
        self.seen: list[ToolCallFinishedEvent] = []

    async def handle(self, event) -> None:
        if isinstance(event, ToolCallFinishedEvent):
            self.seen.append(event)


def build(
    tool: BaseTool,
    *,
    hook_manager: HookManager | None = None,
    config: PermissionConfig | None = None,
    telemetry: InlineTelemetry | None = None,
) -> ToolExecutor:
    assert _workspace_store is not None
    executor = ToolExecutor(
        "sess",
        tools=None,
        telemetry=telemetry,
        tool_call_policy=build_tool_call_policy(
            config,
            hook_manager=hook_manager,
        ),
        tool_result_policy=build_tool_result_policy(hook_manager=hook_manager),
        workspace_store=_workspace_store,
    )
    tool.bind("sess")
    executor.register_native_tool(native_definition(type(tool)), tool)
    return executor


async def test_no_hook_manager_runs_normally():
    tool = SpyTool()
    result = await build(tool).run_command("Spy", {"cmd": "ls"})
    assert result.success and tool.ran and result.output == "ran:ls"


async def test_pre_tool_use_deny_blocks_before_invocation():
    tool = SpyTool()
    manager = HookManager()
    manager.register(
        "PreToolUse",
        lambda _input: {"decision": "block", "reason": "no bash"},
    )
    result = await build(tool, hook_manager=manager).run_command("Spy", {"cmd": "ls"})
    assert not result.success
    assert not tool.ran
    assert "no bash" in result.output


async def test_pre_tool_use_rewrite_reaches_permission_and_tool():
    tool = SpyTool()
    manager = HookManager()
    manager.register(
        "PreToolUse",
        lambda _input: {"updatedInput": {"cmd": "safe"}},
    )
    result = await build(tool, hook_manager=manager).run_command("Spy", {"cmd": "danger"})
    assert result.success
    assert tool.seen_cmd == "safe"


async def test_post_tool_use_rewrite_and_enrichment_are_presentation_only():
    tool = SpyTool()
    manager = HookManager()
    manager.register(
        "PostToolUse",
        lambda _input: {
            "updatedResponse": "[safe representation]",
            "additionalContext": "note: reviewed",
        },
    )
    result = await build(tool, hook_manager=manager).run_command("Spy", {"cmd": "ls"})
    assert result.success
    assert result.output == "[safe representation]\nnote: reviewed"


async def test_post_tool_use_deny_withholds_output_without_rewriting_execution_truth():
    tool = SpyTool()
    manager = HookManager()
    manager.register(
        "PostToolUse",
        lambda _input: {"decision": "block", "reason": "unsafe output"},
    )
    result = await build(tool, hook_manager=manager).run_command("Spy", {"cmd": "ls"})
    assert tool.ran
    assert result.success
    assert result.output == "[PostToolUse] unsafe output"
    assert "ran:ls" not in result.output


@pytest.mark.parametrize(
    ("tool", "name", "args", "expected"),
    [
        (SpyTool(), "Spy", {"cmd": "ls"}, "succeeded"),
        (RaiseTool(), "Raise", {}, "failed"),
    ],
)
async def test_finished_event_carries_immutable_execution_outcome(
    tool: BaseTool,
    name: str,
    args: dict[str, Any],
    expected: str,
):
    observer = FinishedObserver()
    telemetry = InlineTelemetry(observer)
    result = await build(tool, telemetry=telemetry).run_command(name, args)
    assert len(observer.seen) == 1
    assert observer.seen[0].outcome == expected
    assert observer.seen[0].error is result.error


async def test_preflight_rejection_is_explicitly_not_an_execution_failure():
    observer = FinishedObserver()
    telemetry = InlineTelemetry(observer)
    manager = HookManager()
    manager.register("PreToolUse", lambda _input: {"decision": "block"})
    tool = SpyTool()
    await build(tool, hook_manager=manager, telemetry=telemetry).run_command("Spy", {"cmd": "ls"})
    assert not tool.ran
    assert observer.seen[0].outcome == "rejected"


async def test_post_tool_use_hook_receives_structured_execution_fact():
    seen: list[dict] = []
    manager = HookManager()
    manager.register(
        "PostToolUse",
        lambda hook_input: seen.append({"success": hook_input.payload.success, "error": hook_input.payload.error})
        or {},
    )
    result = await build(RaiseTool(), hook_manager=manager).run_command("Raise", {})
    assert not result.success
    assert seen[0]["success"] is False
    assert seen[0]["error"]["error"] == "ToolError"
    assert seen[0]["error"]["message"] == "kaboom"


async def test_alias_is_canonicalized_before_both_policies():
    names: list[str] = []
    manager = HookManager()
    manager.register(
        "PostToolUse",
        lambda hook_input: names.append(hook_input.payload.tool_name) or {},
    )
    tool = AliasSpyTool()
    result = await build(tool, hook_manager=manager).run_command("bash", {})
    assert result.success and tool.ran
    assert names == ["Bash"]


async def test_hook_deny_remains_stricter_than_permission_allow():
    tool = SpyTool()
    manager = HookManager()
    manager.register(
        "PreToolUse",
        lambda _input: {"decision": "block", "reason": "hook veto"},
    )
    result = await build(
        tool,
        hook_manager=manager,
        config=PermissionConfig(allow=["Spy"]),
    ).run_command("Spy", {"cmd": "ls"})
    assert not result.success
    assert not tool.ran
