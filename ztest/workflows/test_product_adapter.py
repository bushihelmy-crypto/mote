from __future__ import annotations

import asyncio

import pytest

from mote.orchestration.background_tasks.operation import (
    OperationCancelled,
    OperationPaused,
    OperationSucceeded,
    StopDisposition,
    StopReason,
)
from mote.orchestration.workflows import WorkflowBuilder
from mote.orchestration.workflows.types import END, START, GraphState, Stage
from mote.product.workflows import (
    AlreadyConsumed,
    WorkflowContinuationRegistry,
    WorkflowInspectionPort,
    WorkflowTaskAdapter,
)


class State(GraphState):
    value: int = 0


async def increment(state: State) -> Stage:
    async def submit():
        return {"value": state.value + 1}

    return Stage(submit=submit())


def definition():
    builder = WorkflowBuilder("adapter", state_schema=State)
    builder.add_node("increment", increment)
    builder.add_edge(START, "increment")
    builder.add_edge("increment", END)
    return builder.build()


@pytest.mark.asyncio
async def test_adapter_maps_success_and_closes_idempotently():
    registry = WorkflowContinuationRegistry("session")
    adapter = WorkflowTaskAdapter(definition().start({"value": 1}), registry)
    outcome = await adapter.execute()
    assert isinstance(outcome, OperationSucceeded)
    assert outcome.output.value == 2
    await adapter.aclose()
    await adapter.aclose()


def test_continuation_is_single_consume():
    registry = WorkflowContinuationRegistry("session")
    run = definition().start({"value": 1})
    ref = registry.register(run.continuation())
    resumed = registry.consume(ref, {"value": 4})
    assert resumed.snapshot().state["value"] == 4
    with pytest.raises(AlreadyConsumed):
        registry.consume(ref)


@pytest.mark.asyncio
async def test_continuation_preserves_node_level_resume_controls():
    registry = WorkflowContinuationRegistry("session")
    run = definition().start({"value": 1})
    initial = await run.execute()
    assert initial.output.value == 2
    ref = registry.register(run.continuation())
    resumed = registry.consume(ref, {"value": 4}, from_nodes=("increment",))

    outcome = await WorkflowTaskAdapter(resumed, registry).execute()

    assert isinstance(outcome, OperationSucceeded)
    assert outcome.output["value"] == 5


def test_inspection_returns_deep_frozen_snapshot():
    run = definition().start({"value": 1, "nested": {"items": [1, 2]}})
    inspection = WorkflowInspectionPort()
    inspection.register("task", run)
    snapshot = inspection.snapshot("task")
    assert snapshot is not None
    assert snapshot.state["nested"]["items"] == (1, 2)
    with pytest.raises(TypeError):
        snapshot.state["nested"]["new"] = True


@pytest.mark.asyncio
async def test_stop_and_execute_observe_one_terminal_with_resume_ref():
    started = asyncio.Event()

    async def blocked(state: State) -> Stage:
        async def submit():
            started.set()
            await asyncio.Event().wait()

        return Stage(submit=submit())

    builder = WorkflowBuilder("blocked", state_schema=State)
    builder.add_node("blocked", blocked)
    builder.add_edge(START, "blocked")
    builder.add_edge("blocked", END)
    registry = WorkflowContinuationRegistry("session")
    adapter = WorkflowTaskAdapter(builder.build().start({"value": 1}), registry)
    execution = asyncio.create_task(adapter.execute())
    await started.wait()
    stopped = await adapter.request_stop(StopReason.USER_CANCEL, StopDisposition.CHECKPOINT)
    completed = await execution
    assert stopped == completed
    assert isinstance(completed, OperationCancelled)
    assert completed.resume_ref is not None


@pytest.mark.asyncio
async def test_workflow_pause_maps_to_opaque_resume_ref():
    builder = WorkflowBuilder("pause", state_schema=State)
    builder.add_node("increment", increment)
    builder.add_node("again", increment)
    builder.add_edge(START, "increment")
    builder.add_llm_edges("increment", "continue?", {"yes": "again", "no": END})
    registry = WorkflowContinuationRegistry("session")
    adapter = WorkflowTaskAdapter(builder.build().start({"value": 1}), registry)
    outcome = await adapter.execute()
    assert isinstance(outcome, OperationPaused)
    assert outcome.resume_ref.value.startswith("wfr_1_")
