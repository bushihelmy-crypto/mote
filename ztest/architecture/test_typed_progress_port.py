from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from mote.contracts.task.lifecycle import BackgroundTaskOwner, LocalTaskReference
from mote.contracts.task.models import AttemptId, TaskId
from mote.contracts.task.progress import ActivityProgressEvent, ActivityProgressIdentity, ProgressEvent, ProgressPhase
from mote.orchestration.background_tasks.delivery import make_progress_sink
from mote.orchestration.workflows.events import report_progress
from mote.runtime.events.progress_scope import bind_progress_sink, current_progress_sink, reset_progress_sink


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[ProgressEvent] = []

    def emit(self, event: ProgressEvent) -> None:
        self.events.append(event)


def _event(run_id: str, detail: str | None = None) -> ActivityProgressEvent:
    return ActivityProgressEvent(
        ActivityProgressIdentity(run_id, "mote.workflow.v1.sha256-" + "a" * 64),
        "node",
        ProgressPhase.RUNNING,
        detail,
    )


@pytest.mark.asyncio
async def test_concurrent_task_bindings_do_not_cross_stream() -> None:
    first = _RecordingSink()
    second = _RecordingSink()

    async def emit_one(sink: _RecordingSink, run_id: str) -> None:
        token = bind_progress_sink(sink)
        try:
            await asyncio.sleep(0)
            report_progress(_event(run_id))
        finally:
            reset_progress_sink(token)

    await asyncio.gather(
        emit_one(first, "run-1"),
        emit_one(second, "run-2"),
    )
    assert [event.identity.execution_id for event in first.events if isinstance(event, ActivityProgressEvent)] == [
        "run-1"
    ]
    assert [event.identity.execution_id for event in second.events if isinstance(event, ActivityProgressEvent)] == [
        "run-2"
    ]
    assert current_progress_sink() is None


def test_sink_exception_and_unbind_do_not_leak_binding() -> None:
    class _FailingSink:
        def emit(self, event: ProgressEvent) -> None:
            raise OSError("observation failed")

    sink = _FailingSink()
    token = bind_progress_sink(sink)
    try:
        report_progress(_event("run-failure"))
        assert current_progress_sink() is sink
    finally:
        reset_progress_sink(token)
    assert current_progress_sink() is None


@pytest.mark.asyncio
async def test_cancelled_task_resets_its_progress_binding() -> None:
    sink = _RecordingSink()
    entered = asyncio.Event()

    async def bound_task() -> None:
        token = bind_progress_sink(sink)
        try:
            entered.set()
            await asyncio.Future()
        finally:
            reset_progress_sink(token)

    task = asyncio.create_task(bound_task())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert current_progress_sink() is None


def test_background_projection_keeps_only_local_task_identity(monkeypatch) -> None:
    import mote.orchestration.background_tasks.delivery as delivery_module

    telemetry = []
    monkeypatch.setattr(delivery_module, "observe_event_sync", telemetry.append)
    owner = BackgroundTaskOwner("process", "agent", "incarnation")
    reference = LocalTaskReference(owner, TaskId("bg_7"), AttemptId(3))
    lines: list[str] = []
    notifications = []
    sink = make_progress_sink(
        lines.append,
        reference=reference,
        command_name="workflow",
        deliver=notifications.append,
    )
    event = ActivityProgressEvent(
        ActivityProgressIdentity("run-7", "definition-7"),
        "node-7",
        ProgressPhase.FAILED,
        "failed detail",
    )
    sink.emit(event)

    assert lines == ["[node-7] failed: failed detail\n"]
    assert len(notifications) == 1
    assert notifications[0].task_id == "bg_7"
    assert notifications[0].attempt_id == AttemptId(3)
    routed = telemetry[0].progress
    assert routed.reference == reference
    assert routed.stage == "node-7"
    assert routed.phase is ProgressPhase.FAILED
    assert routed.detail == "failed detail"


def test_runtime_binding_has_no_workflow_state_machine_or_any_callback() -> None:
    runtime_source = Path("runtime/events/progress_scope.py").read_text(encoding="utf-8")
    contract_source = Path("contracts/task/progress.py").read_text(encoding="utf-8")
    assert "orchestration" not in runtime_source
    assert "WorkflowNodeStatus" not in runtime_source
    assert "Callable[[str, Any, Any]" not in runtime_source
    assert "Any" not in contract_source
    assert "class ProgressEventSink(Protocol)" in contract_source
