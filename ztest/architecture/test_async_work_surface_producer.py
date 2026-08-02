from __future__ import annotations

import json

from mote.contracts.async_work.codec import decode_async_work_observation
from mote.contracts.async_work.identity import LocalBackgroundTaskReference
from mote.contracts.async_work.observation import (
    AsyncWorkAction,
    AsyncWorkPresentationPhase,
    LocalBackgroundObservationDetail,
    LocalBackgroundTaskObservation,
)
from mote.contracts.ports.async_work.observation import AsyncWorkQueryDisposition, AsyncWorkQueryResult
from mote.contracts.task.lifecycle import BackgroundTaskOwner, LocalTaskReference
from mote.contracts.task.models import AttemptId, TaskId
from mote.product.async_work.service import CurrentAgentAsyncWorkObservationService
from mote.product.presentation.events import AsyncWorkObserved


class _Port:
    def __init__(self, result: AsyncWorkQueryResult) -> None:
        self._result = result

    def get(self, reference):
        return self._result


def test_current_agent_query_emits_strict_surface_observation(monkeypatch) -> None:
    reference = LocalBackgroundTaskReference(
        LocalTaskReference(
            BackgroundTaskOwner("process", "agent", "incarnation"),
            TaskId("task"),
            AttemptId(1),
        )
    )
    observation = LocalBackgroundTaskObservation(
        reference,
        AsyncWorkPresentationPhase.RUNNING,
        LocalBackgroundObservationDetail("command", True, True),
        None,
        (AsyncWorkAction.CANCEL,),
    )
    events: list[object] = []
    monkeypatch.setattr("mote.product.async_work.service.observe_event_sync", events.append)
    port = _Port(AsyncWorkQueryResult(AsyncWorkQueryDisposition.FOUND, observation))
    result = CurrentAgentAsyncWorkObservationService(port, port).get(reference)
    assert result.observation == observation
    assert len(events) == 1 and isinstance(events[0], AsyncWorkObserved)
    event = events[0]
    assert isinstance(event, AsyncWorkObserved)
    assert decode_async_work_observation(json.loads(event.observation_json)) == observation
    assert event.observation_json == json.dumps(
        json.loads(event.observation_json), sort_keys=True, separators=(",", ":")
    )
