"""Contract tests for the closed ToolExecutionPipeline stage order."""

from __future__ import annotations

import pytest

from mote.executor.tool_pipeline import ToolExecutionPipeline
from mote.executor.tool_result import ToolResult


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

    async def finish(self, *args):
        self._events.append("settle")
        return args[2]


def _pipeline(events, *, authorize=None, ledger=None):
    pipeline = ToolExecutionPipeline.__new__(ToolExecutionPipeline)
    pipeline._resolve = _Stage(events, "resolve")
    pipeline._authorize = _Stage(events, "authorize", authorize, async_run=True)
    pipeline._ledger = _Stage(events, "ledger", ledger)
    pipeline._invoke = _Stage(events, "invoke", ToolResult(output="ok"), async_run=True)
    pipeline._settlement = _Settlement(events)
    return pipeline


@pytest.mark.asyncio
async def test_success_order_is_resolve_authorize_ledger_invoke_settle():
    events: list[str] = []
    result = await _pipeline(events).run("Echo", {}, "call-1")
    assert result.output == "ok"
    assert events == ["resolve", "authorize", "ledger", "invoke", "settle"]


@pytest.mark.asyncio
async def test_authorize_rejection_cannot_reach_ledger_or_invoke():
    events: list[str] = []
    denied = ToolResult(output="denied", success=False)
    result = await _pipeline(events, authorize=denied).run("Echo", {}, "call-1")
    assert result is denied
    assert events == ["resolve", "authorize", "reject"]


@pytest.mark.asyncio
async def test_ledger_short_circuit_cannot_invoke_or_settle_as_ran():
    events: list[str] = []
    replay = ToolResult(output="replayed")
    result = await _pipeline(events, ledger=replay).run("Echo", {}, "call-1")
    assert result is replay
    assert events == ["resolve", "authorize", "ledger", "reject"]
