"""Local CancelTasks tests; durable Workflow resume has a distinct capability."""

from __future__ import annotations

import asyncio

import pytest

from mote.contracts.conversation import MessageQueue
from mote.contracts.session.identity import SessionId
from mote.contracts.tool.errors import ToolError
from mote.orchestration.background_tasks.pool import BackgroundTaskPool
from mote.product.agents.background_tasks import AgentBackgroundTasks
from mote.product.toolsets.builtin.cancel_tasks import CancelTasks

pytestmark = pytest.mark.asyncio


@pytest.fixture
def pool():
    messages = MessageQueue()
    return AgentBackgroundTasks(
        BackgroundTaskPool(messages, max_concurrency=10),
        SessionId("test-cancel"),
        messages,
    )


def _tool(pool: AgentBackgroundTasks) -> CancelTasks:
    tool = CancelTasks()
    tool.get_bg_pool = lambda: pool
    return tool


async def test_unknown_task_raises(pool) -> None:
    with pytest.raises(ToolError, match="Unknown task_id"):
        await _tool(pool).call(task_id="bg_999")


async def test_cancel_running_task(pool) -> None:
    gate = asyncio.Event()

    async def slow():
        await gate.wait()
        return "done"

    task_id = pool.submit(lambda: slow(), "slow-task", timeout=None)
    await asyncio.sleep(0)
    result = await _tool(pool).call(task_id=task_id, reason="no longer needed")
    assert "cancelled" in result.lower()
    assert "no longer needed" in result


async def test_cancel_already_done(pool) -> None:
    async def instant():
        return "done"

    task_id = pool.submit(lambda: instant(), "fast-task", timeout=5)
    await pool.wait_all()
    assert "already" in (await _tool(pool).call(task_id=task_id)).lower()
