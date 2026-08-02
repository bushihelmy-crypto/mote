from __future__ import annotations

import asyncio

import pytest

from mote.contracts.async_work.command import CancelLocalBackgroundTask, LocalCancelDisposition
from mote.contracts.async_work.identity import LocalBackgroundTaskReference
from mote.contracts.async_work.observation import AsyncWorkPresentationPhase
from mote.contracts.ports.async_work import AsyncWorkQueryDisposition
from mote.orchestration.background_tasks.observation import AgentOwnedLocalAsyncWorkAdapter

from .conftest import forever


@pytest.mark.asyncio
async def test_local_observation_and_cancel_bind_full_attempt_reference(pool) -> None:
    acceptance = pool.submit(lambda: forever(), "observe", timeout=None)
    await asyncio.sleep(0)
    reference = LocalBackgroundTaskReference(acceptance.reference)
    adapter = AgentOwnedLocalAsyncWorkAdapter(pool)

    query = adapter.get(reference)
    assert query.disposition is AsyncWorkQueryDisposition.FOUND
    observation = query.observation
    assert observation is not None
    assert observation.reference == reference
    assert observation.phase is AsyncWorkPresentationPhase.RUNNING
    assert observation.detail.pinned is True

    receipt = adapter.cancel(CancelLocalBackgroundTask(reference, "user"))
    assert receipt.disposition is LocalCancelDisposition.CANCEL_REQUESTED
    await pool.wait_all()


@pytest.mark.asyncio
async def test_local_observation_rejects_foreign_owner(pool) -> None:
    acceptance = pool.submit(lambda: forever(), "observe", timeout=None)
    local = acceptance.reference
    foreign = type(local.owner)("other-process", local.owner.agent_id, local.owner.incarnation_id)
    reference = LocalBackgroundTaskReference(type(local)(foreign, local.task_id, local.attempt_id))
    adapter = AgentOwnedLocalAsyncWorkAdapter(pool)
    assert adapter.get(reference).disposition is AsyncWorkQueryDisposition.OWNER_LOST
    receipt = adapter.cancel(CancelLocalBackgroundTask(reference, "user"))
    assert receipt.disposition is LocalCancelDisposition.OWNER_LOST
    pool.cancel(str(local.task_id))
    await pool.wait_all()
