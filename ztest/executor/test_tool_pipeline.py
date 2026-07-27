"""Contract tests for the closed ToolExecutionPipeline stage order."""

from __future__ import annotations

import pytest

from mote.runtime.tools.tool_pipeline import InvokeStage, LedgerStage, ToolExecution, ToolExecutionPipeline
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


def _pipeline(events, *, authorize=None, ledger=None):
    pipeline = ToolExecutionPipeline.__new__(ToolExecutionPipeline)
    pipeline._resolve = _Stage(events, "resolve")
    pipeline._authorize = _Stage(events, "authorize", authorize, async_run=True)
    pipeline._ledger = _Stage(events, "ledger", ledger)
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
        "ledger",
        "start",
        "invoke",
        "settle",
    ]


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


@pytest.mark.asyncio
async def test_invoke_binds_and_restores_ambient_tool_call_id():
    from mote.runtime.errors import RecoveryRunner
    from mote.runtime.tools.execution_context import current_tool_call_id

    seen = []

    class Tool:
        async def call(self):
            seen.append(current_tool_call_id())
            return "ok"

    stage = InvokeStage(RecoveryRunner({}), None)
    result = await stage.run(ToolExecution(name="Probe", args={}, result_id="stable-call", tool=Tool()))

    assert result.output == "ok"
    assert seen == ["stable-call"]
    assert current_tool_call_id() is None


def test_started_external_call_reenters_only_for_explicit_reconciliation():
    from types import SimpleNamespace

    from mote.contracts.tools.effects import ToolEffect

    class Ledger:
        def __init__(self):
            self.started = 0

        def status(self, _call_id):
            return SimpleNamespace(status="started", effect="external")

        def mark_started(self, *_args, **_kwargs):
            self.started += 1

    class Tool:
        @classmethod
        def resolve_effect(cls):
            return ToolEffect.EXTERNAL

        def resolve_effect_for(self, args):
            return self.resolve_effect()

        def can_resume_started_call(self, call_id):
            return call_id == "recoverable"

    ledger = Ledger()
    stage = LedgerStage(ledger)

    recoverable = ToolExecution(name="RunGraph", args={}, result_id="recoverable", tool=Tool())
    blocked = ToolExecution(name="External", args={}, result_id="unknown", tool=Tool())

    assert stage.run(recoverable) is None
    assert stage.run(blocked).success is False
    assert ledger.started == 0
