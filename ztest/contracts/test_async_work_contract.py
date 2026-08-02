from __future__ import annotations

import pytest

from mote.contracts.async_work.codec import (
    decode_async_work_observation,
    decode_async_work_reference,
    encode_async_work_observation,
    encode_async_work_reference,
)
from mote.contracts.async_work.identity import DurableWorkflowRunReference, LocalBackgroundTaskReference
from mote.contracts.async_work.observation import (
    AsyncWorkAction,
    AsyncWorkPresentationPhase,
    LocalBackgroundObservationDetail,
    LocalBackgroundTaskObservation,
)
from mote.contracts.task.lifecycle import BackgroundTaskOwner, LocalTaskReference
from mote.contracts.task.models import AttemptId, TaskId
from mote.contracts.workflow.identity import WorkflowDefinitionId, WorkflowRunId, WorkflowRunReference


def test_reference_codec_preserves_local_nominal_identity() -> None:
    reference = LocalBackgroundTaskReference(
        LocalTaskReference(
            BackgroundTaskOwner("process", "agent", "incarnation"),
            TaskId("task"),
            AttemptId(2),
        )
    )
    assert decode_async_work_reference(encode_async_work_reference(reference)) == reference


def test_reference_codec_preserves_workflow_nominal_identity() -> None:
    reference = DurableWorkflowRunReference(
        WorkflowRunReference(WorkflowRunId("wfr_1"), WorkflowDefinitionId("definition_1"))
    )
    decoded = decode_async_work_reference(encode_async_work_reference(reference))
    assert decoded == reference
    assert isinstance(decoded.reference.run_id, WorkflowRunId)
    assert isinstance(decoded.reference.definition_id, WorkflowDefinitionId)


@pytest.mark.parametrize(
    "payload",
    [
        {"schema": "unknown", "kind": "durable_workflow_run", "payload": {}},
        {
            "schema": "mote.async-work-observation/v1",
            "kind": "durable_workflow_run",
            "payload": {"run_id": "run", "definition_id": "definition", "task_id": "x"},
        },
        {
            "schema": "mote.async-work-observation/v1",
            "kind": "local_background_task",
            "payload": {
                "process_instance_id": "process",
                "agent_id": "agent",
                "incarnation_id": "incarnation",
                "task_id": "task",
                "attempt_id": "1",
            },
        },
    ],
)
def test_reference_codec_fails_closed(payload: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        decode_async_work_reference(payload)


def test_observation_codec_round_trips_and_rejects_extra_fields() -> None:
    reference = LocalBackgroundTaskReference(
        LocalTaskReference(
            BackgroundTaskOwner("process", "agent", "incarnation"),
            TaskId("task"),
            AttemptId(2),
        )
    )
    observation = LocalBackgroundTaskObservation(
        reference,
        AsyncWorkPresentationPhase.RUNNING,
        LocalBackgroundObservationDetail("command", True, True),
        None,
        (AsyncWorkAction.CANCEL,),
    )
    encoded = encode_async_work_observation(observation)
    assert decode_async_work_observation(encoded) == observation
    encoded["extra"] = True
    with pytest.raises(ValueError):
        decode_async_work_observation(encoded)
