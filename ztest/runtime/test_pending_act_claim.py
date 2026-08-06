from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from mote.contracts.events.pending_act import PendingActCreatedEvent
from mote.contracts.execution.pending_act import (
    PendingActFrontier,
    PendingActFrontierId,
    PendingAction,
    ToolCompositionDefinitionRef,
)
from mote.contracts.ports.events.journal import StreamWriterFence
from mote.contracts.tool import ToolEffect, ToolInvocationId
from mote.runtime.session.events import SessionMetaEvent
from mote.runtime.session.log import SessionLog
from mote.runtime.session.pending_act_claim import PendingActClaimService
from mote.runtime.session.projection import SessionLiveProjection


class _ApplyingSink:
    def __init__(self, projection: SessionLiveProjection, log: SessionLog) -> None:
        self._projection = projection
        self._log = log

    async def commit_guarded(self, batch):
        for event in batch.events:
            self._log.commit_offline(event)
        self._projection.restore(self._log.iter_events())
        return object()


def _frontier() -> PendingActFrontier:
    return PendingActFrontier(
        1,
        PendingActFrontierId("frontier-1"),
        "session-1",
        "run-1",
        "call-1",
        0,
        ToolCompositionDefinitionRef("agent", "1", "sha", "generation", "catalog", "provider", "policy", "capability"),
        (
            PendingAction(
                0, ToolInvocationId("invocation-1"), "action-1", "External", "external/v1", 1, ToolEffect.EXTERNAL, 0
            ),
        ),
    )


@pytest.mark.asyncio
async def test_claim_renew_keeps_fence_takeover_advances_and_stales_old_permit(tmp_path) -> None:
    log = SessionLog("session-1", base_dir=str(tmp_path))
    frontier = _frontier()
    log.commit_offline(SessionMetaEvent("session-1", "test.Role", ()))
    log.commit_offline(PendingActCreatedEvent(frontier))
    projection = SessionLiveProjection(log.stream_id)
    projection.restore(log.iter_events())
    service = PendingActClaimService(projection, _ApplyingSink(projection, log))
    writer = StreamWriterFence("run-1", "worker-1", "incarnation-1", 1)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    first = await service.acquire(
        frontier.frontier_id,
        "worker-1",
        "incarnation-1",
        acquired_at=now,
        expires_at=now + timedelta(seconds=10),
        expected_stream_version=2,
        writer=writer,
    )
    renewed = await service.renew(
        first,
        expires_at=now + timedelta(seconds=20),
        expected_stream_version=3,
        writer=writer,
    )
    assert renewed.claim_revision == first.claim_revision + 1
    assert renewed.fencing_token == first.fencing_token
    permit = service.begin_invoke(
        renewed,
        frontier.actions[0].invocation_id,
        frontier_revision=0,
        expected_stream_version=4,
        at=now + timedelta(seconds=1),
    )
    taken = await service.takeover(
        frontier.frontier_id,
        "worker-2",
        "incarnation-2",
        acquired_at=now + timedelta(seconds=20),
        expires_at=now + timedelta(seconds=40),
        expected_stream_version=4,
        writer=writer,
    )
    assert taken.fencing_token == permit.fencing_token + 1
    with pytest.raises(ValueError, match="stale"):
        service.begin_invoke(
            renewed,
            frontier.actions[0].invocation_id,
            frontier_revision=0,
            expected_stream_version=5,
            at=now + timedelta(seconds=21),
        )
