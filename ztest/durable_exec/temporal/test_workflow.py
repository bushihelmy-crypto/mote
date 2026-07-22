#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""End-to-end Temporal workflow tests for the Tier-2 backend (B5).

These need a RUNNING Temporal server, provided in-memory by
``WorkflowEnvironment.start_time_skipping()`` (downloads a test-server binary once,
then cached). They exercise the IN-WORKFLOW path of ``TemporalBackend.run_step``
that ``test_backend.py`` cannot: the step is dispatched as a real
``workflow.execute_activity``, run by a real ``Worker`` in-process, its result
memoized by Temporal's event history.

What they prove that the inline tests can't:

* the ``run_step`` activity is dispatchable from inside a workflow and returns the
  closure's payload across the (pydantic-data-converter) activity boundary;
* the EXTERNAL belt-and-suspenders holds INSIDE the activity — a pre-seeded
  ``completed`` journal record short-circuits the activity so the closure never
  re-runs (a duplicate side effect is impossible even if Temporal ever re-dispatches
  before committing);
* the journal is driven from inside the activity (disk I/O stays out of workflow
  code), recording ``completed`` with the closure's payload.

Gated on ``temporalio`` importability — the whole file skips when the ``[temporal]``
extra is absent, so the core test env stays green. An ``UnsandboxedWorkflowRunner``
is used deliberately: these assert the durable-step semantics, not the workflow
determinism sandbox (that passthrough config is a plugin-lifecycle concern, exercised
separately), so the simpler runner keeps the test focused and fast.
"""
from __future__ import annotations

import uuid
from typing import Awaitable, Callable, Optional

import pytest

pytest.importorskip("temporalio")

from temporalio import workflow
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from mote.common.ledger import COMPLETED, KIND_TOOL, RunJournal
from mote.common.schema import TemporalConfig
from mote.common.workspace import WorkspaceStore
from mote.durable_exec.temporal import TemporalBackend, data_converter

TASK_QUEUE = "mote-test-tq"

# Module-level holder the workflow reads. A Temporal workflow must be defined at
# module scope (the class is registered on a worker), so the per-test backend +
# closure are injected through this holder right before the worker starts — the
# worker runs the workflow in THIS process, so the holder is visible to it.
_BACKEND: Optional[TemporalBackend] = None
_CLOSURE: Optional[Callable[[], Awaitable[str]]] = None
_CALLS: list[int] = []


def _make_closure(payload: str) -> Callable[[], Awaitable[str]]:
    async def _closure() -> str:
        _CALLS.append(1)
        return payload

    return _closure


@workflow.defn
class _RunStepWorkflow:
    """A minimal workflow driving ONE durable step through the backend.

    Mirrors how the real loop calls the backend from inside a workflow: the
    backend registers the closure process-locally then dispatches the generic
    ``run_step`` activity, whose result Temporal memoizes.
    """

    @workflow.run
    async def run(self, step_id: str) -> str:
        assert _BACKEND is not None and _CLOSURE is not None
        return await _BACKEND.run_step(step_id, KIND_TOOL, "external", _CLOSURE, name="Bash", tool_call_id=step_id)


def _journal(tmp_path, session_id="wf-sess") -> RunJournal:
    return RunJournal(session_id, store=WorkspaceStore(root=str(tmp_path)))


def _client(env: WorkflowEnvironment) -> Client:
    # Reuse the env's live service connection but swap in mote's pydantic data
    # converter so StepInput serializes across the activity boundary exactly as
    # production does. Rebuilding from the same service_client avoids re-dialing.
    cfg = env.client.config()
    return Client(
        env.client.service_client,
        namespace=cfg["namespace"],
        data_converter=data_converter,
    )


@pytest.mark.asyncio
async def test_in_workflow_dispatches_activity_and_records_completed(tmp_path):
    global _BACKEND, _CLOSURE
    _CALLS.clear()
    journal = _journal(tmp_path)
    _BACKEND = TemporalBackend(TemporalConfig(task_queue=TASK_QUEUE), journal)
    _CLOSURE = _make_closure("workflow result")

    async with await WorkflowEnvironment.start_time_skipping() as env:
        client = _client(env)
        async with Worker(
            client,
            task_queue=TASK_QUEUE,
            workflows=[_RunStepWorkflow],
            activities=_BACKEND.temporal_activities,
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            step_id = "tool:" + uuid.uuid4().hex
            out = await client.execute_workflow(
                _RunStepWorkflow.run,
                args=[step_id],
                id="wf-" + uuid.uuid4().hex,
                task_queue=TASK_QUEUE,
            )

    # The activity ran the closure exactly once and returned its payload.
    assert out == "workflow result"
    assert _CALLS == [1]
    # The journal was driven from INSIDE the activity (I/O out of workflow code).
    rec = journal.replay(step_id)
    assert rec is not None
    assert rec.status == COMPLETED
    assert rec.payload == "workflow result"
    assert rec.kind == KIND_TOOL
    assert rec.effect == "external"


@pytest.mark.asyncio
async def test_in_workflow_completed_record_short_circuits_the_closure(tmp_path):
    # EXTERNAL belt-and-suspenders: a pre-existing completed record makes the
    # activity replay the recorded payload WITHOUT re-running the closure — a
    # duplicate side effect is impossible even inside a live workflow dispatch.
    global _BACKEND, _CLOSURE
    _CALLS.clear()
    journal = _journal(tmp_path, session_id="wf-sess-2")
    step_id = "tool:" + uuid.uuid4().hex
    # Seed a completed record for this step (as a prior run would have left).
    journal.record_started(step_id, KIND_TOOL, "external", name="Bash", tool_call_id=step_id)
    journal.record_completed(step_id, payload="already done")

    _BACKEND = TemporalBackend(TemporalConfig(task_queue=TASK_QUEUE), journal)
    _CLOSURE = _make_closure("SHOULD NOT RUN")

    async with await WorkflowEnvironment.start_time_skipping() as env:
        client = _client(env)
        async with Worker(
            client,
            task_queue=TASK_QUEUE,
            workflows=[_RunStepWorkflow],
            activities=_BACKEND.temporal_activities,
            workflow_runner=UnsandboxedWorkflowRunner(),
        ):
            out = await client.execute_workflow(
                _RunStepWorkflow.run,
                args=[step_id],
                id="wf-" + uuid.uuid4().hex,
                task_queue=TASK_QUEUE,
            )

    assert out == "already done"
    assert _CALLS == []  # closure never re-ran — idempotency held inside the activity
