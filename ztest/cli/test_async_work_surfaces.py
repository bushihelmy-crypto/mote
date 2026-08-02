from __future__ import annotations

import json

from mote.contracts.async_work.codec import encode_async_work_observation
from mote.contracts.async_work.identity import DurableWorkflowRunReference
from mote.contracts.async_work.observation import (
    AsyncWorkAction,
    AsyncWorkPresentationPhase,
    DurableWorkflowObservationDetail,
    DurableWorkflowRunObservation,
)
from mote.contracts.workflow.identity import WorkflowDefinitionId, WorkflowRunId, WorkflowRunReference
from mote.product.interfaces.acp import wire as acp
from mote.product.interfaces.agui import wire as agui
from mote.product.presentation.events import AsyncWorkObserved
from mote.product.presentation.state.ops import RenderTaskProgress
from mote.product.presentation.state.reducer import TranscriptReducer


def _event() -> AsyncWorkObserved:
    observation = DurableWorkflowRunObservation(
        DurableWorkflowRunReference(WorkflowRunReference(WorkflowRunId("run-1"), WorkflowDefinitionId("definition-1"))),
        7,
        AsyncWorkPresentationPhase.PAUSED,
        DurableWorkflowObservationDetail(None),
        ("node",),
        None,
        None,
        (AsyncWorkAction.RESUME, AsyncWorkAction.CANCEL),
        (),
    )
    return AsyncWorkObserved(observation_json=json.dumps(encode_async_work_observation(observation)))


def test_terminal_and_textual_projection_preserve_workflow_badge_ids_and_actions() -> None:
    operations = TranscriptReducer().feed(_event())
    rendered = next(operation for operation in operations if isinstance(operation, RenderTaskProgress))
    assert rendered.ev.status == "paused"
    assert "durable workflow" in rendered.ev.detail
    assert "run_id=run-1" in rendered.ev.detail
    assert "definition_id=definition-1" in rendered.ev.detail
    assert "actions=resume,cancel" in rendered.ev.detail


def test_acp_and_agui_preserve_strict_tagged_observation() -> None:
    acp_output = acp.to_acp_updates(_event(), acp.AcpWireState("session"))[0]
    assert acp_output["sessionUpdate"] == "mote_async_work_observation"
    assert acp_output["observation"]["kind"] == "durable_workflow_run"
    agui_output = agui.to_agui_events(_event(), agui.AguiWireState("thread", "run"))[0]
    assert agui_output["type"] == "CUSTOM"
    assert agui_output["name"] == "async_work_observation"
    assert agui_output["value"]["kind"] == "durable_workflow_run"
