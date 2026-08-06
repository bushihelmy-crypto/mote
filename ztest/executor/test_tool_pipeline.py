"""Contract tests for the closed ToolExecutionPipeline stage order."""

from __future__ import annotations

import pytest

from mote.runtime.tools.tool_pipeline import InvokeStage, ToolExecution, ToolExecutionPipeline
from mote.runtime.tools.tool_result import ToolResult


class _Stage:
    def __init__(self, events: list[str], name: str, result=None, *, async_run: bool = False):
        self._events = events
        self._name = name
        self._result = result
        self._async = async_run

    def run(self, execution):
        self._events.append(self._name)
        if not self._async:
            return self._result

        async def completed():
            return self._result

        return completed()


class _Settlement:
    def __init__(self, events: list[str]):
        self._events = events

    async def reject(self, *args):
        self._events.append("reject")
        return args[2]

    async def start(self, *args):
        self._events.append("start")

    async def finish(self, *args):
        self._events.append("settle")
        return args[2]


def _pipeline(events, *, authorize=None):
    pipeline = ToolExecutionPipeline.__new__(ToolExecutionPipeline)
    pipeline._resolve = _Stage(events, "resolve")
    pipeline._authorize = _Stage(events, "authorize", authorize, async_run=True)
    pipeline._invoke = _Stage(events, "invoke", ToolResult(output="ok"), async_run=True)
    pipeline._settlement = _Settlement(events)
    return pipeline


@pytest.mark.asyncio
async def test_success_order_reaches_start_only_at_invocation_boundary():
    events: list[str] = []
    result = await _pipeline(events).run("Echo", {}, "call-1")
    assert result.output == "ok"
    assert events == [
        "resolve",
        "authorize",
        "start",
        "invoke",
        "settle",
    ]


@pytest.mark.asyncio
async def test_authorize_rejection_cannot_reach_invoke():
    events: list[str] = []
    denied = ToolResult(output="denied", success=False)
    result = await _pipeline(events, authorize=denied).run("Echo", {}, "call-1")
    assert result is denied
    assert events == ["resolve", "authorize", "reject"]


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_invoke_binds_and_restores_ambient_tool_call_id():
    from mote.contracts.tool import ToolAttemptOrdinal, ToolInvocationId, ToolInvocationIdentity
    from mote.runtime.resilience.recovery import RecoveryRunner
    from mote.runtime.tools.execution_context import current_tool_call_id

    seen = []

    class Tool:
        from types import SimpleNamespace

        definition = SimpleNamespace(execution_kind="atomic")

        async def call(self):
            seen.append(current_tool_call_id())
            return "ok"

    stage = InvokeStage(RecoveryRunner({}), None)
    identity = ToolInvocationIdentity(
        ToolInvocationId("stable-call"),
        ToolAttemptOrdinal(1),
        "probe/v1",
        1,
        "sha256-arguments",
        "agent-1",
        "run-1",
    )
    result = await stage.run(
        ToolExecution(name="Probe", args={}, identity=identity, tool=Tool(), authorization_generation=1)
    )

    assert result.output == "ok"
    assert seen == ["stable-call"]
    assert current_tool_call_id() is None
