from __future__ import annotations

import asyncio

import pytest

from mote.contracts.conversation import CauseBy, MessageQueue
from mote.contracts.task.models import AttemptId
from mote.orchestration.background_tasks.model import BackgroundTaskNotification
from mote.orchestration.background_tasks.pool import BackgroundTaskPool
from mote.orchestration.background_tasks.status import BackgroundTaskStatus


def _notification(task_id: str, attempt_id: AttemptId) -> BackgroundTaskNotification:
    return BackgroundTaskNotification(
        content="progress",
        cause_by=CauseBy.RUN_COMMAND,
        task_id=task_id,
        attempt_id=attempt_id,
        command_name="attempt",
        status=BackgroundTaskStatus.RUNNING,
    )


def test_attempt_id_is_a_strict_positive_nominal_integer() -> None:
    assert AttemptId(1).value == 1
    for invalid in (0, -1, True, 1.0, "1"):
        with pytest.raises(ValueError):
            AttemptId(invalid)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_resubmit_preserves_identity_and_invalidates_old_attempt() -> None:
    inbox = MessageQueue()
    pool = BackgroundTaskPool(inbox)
    release_first = asyncio.Event()
    release_second = asyncio.Event()

    async def first() -> str:
        await release_first.wait()
        return "stale"

    async def second() -> str:
        await release_second.wait()
        return "current"

    task_id = pool.submit(first, "attempt", timeout=None)
    await asyncio.sleep(0)
    first_task = pool._tasks[task_id]

    assert pool.resubmit(task_id, second, timeout=None, progress=False) == task_id
    current = pool.get_task_info(task_id)
    assert current is not None
    assert current.attempt_id == AttemptId(2)
    assert pool.get_attempt_history(task_id)[0].attempt_id == AttemptId(1)

    pool.deliver(_notification(task_id, AttemptId(1)))
    assert inbox.empty()

    release_first.set()
    await first_task
    await asyncio.sleep(0)
    current = pool.get_task_info(task_id)
    assert current is not None
    assert current.attempt_id == AttemptId(2)
    assert current.status == BackgroundTaskStatus.RUNNING
    assert current.result is None
    assert inbox.empty()

    release_second.set()
    await pool.wait_all()
    current = pool.get_task_info(task_id)
    assert current is not None
    assert current.status == BackgroundTaskStatus.SUCCESS
    assert current.result == "current"
    assert inbox.empty() is False


def test_unknown_task_notification_fails_closed() -> None:
    inbox = MessageQueue()
    pool = BackgroundTaskPool(inbox)
    pool.deliver(_notification("bg_unknown", AttemptId(1)))
    assert inbox.empty()
