from __future__ import annotations

import pytest

from mote.contracts.tool.effects import ToolEffect
from mote.contracts.tool.identity import ToolAttemptOrdinal, ToolInvocationId, ToolInvocationIdentity
from mote.runtime.session.workspace import SessionWorkspace
from mote.runtime.tools.base_tool import BaseTool
from mote.runtime.tools.effect_store import ToolEffectState, ToolEffectStore
from mote.runtime.tools.tool_result_receipt import decode_tool_result_receipt

from .conftest import make_executor


class ExternalEcho(BaseTool):
    name = "ExternalEcho"
    effect = ToolEffect.EXTERNAL

    async def call(self, *, text: str) -> str:
        return text


def _identity(invocation_id: str = "call-1") -> ToolInvocationIdentity:
    return ToolInvocationIdentity(
        invocation_id=ToolInvocationId(invocation_id),
        attempt_ordinal=ToolAttemptOrdinal(1),
        definition_identity="test.echo/v1",
        catalog_generation=1,
        arguments_digest="sha256:" + "0" * 64,
        owner_id="session-1",
        run_id="run-1",
    )


def test_effect_store_strictly_round_trips_and_settles(tmp_path) -> None:
    workspace = SessionWorkspace(tmp_path)
    store = ToolEffectStore("session-1", workspace)
    identity = _identity()
    store.commit_intent(identity, "Echo", ToolEffect.EXTERNAL)
    store.settle("call-1", succeeded=True, receipt="receipt")

    reopened = ToolEffectStore("session-1", workspace)
    record = reopened.lookup("call-1")
    assert record is not None
    assert record.state is ToolEffectState.SUCCEEDED
    assert record.receipt == "receipt"


@pytest.mark.asyncio
async def test_executor_uses_tool_effect_store_not_run_journal(tmp_path) -> None:
    workspace = SessionWorkspace(tmp_path)
    executor = make_executor(ExternalEcho(), session_id="session-1", workspace_store=workspace)
    result = await executor.run_command("ExternalEcho", {"text": "hello"}, result_id="call-1")

    record = executor.effect_store.lookup("call-1") if executor.effect_store is not None else None
    assert record is not None
    assert record.state is ToolEffectState.SUCCEEDED
    assert decode_tool_result_receipt(record.receipt, success=True) == result
