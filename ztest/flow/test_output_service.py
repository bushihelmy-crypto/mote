from __future__ import annotations

from types import SimpleNamespace

import pytest

from mote.contracts.execution.models import MutationResult, MutationStatus
from mote.contracts.model.inference import InferenceResult
from mote.contracts.model.turn import FinalCandidateAction
from mote.contracts.output import AcceptedOutput, OutputEvaluation
from mote.kernel.commands.contracts import HistoryProjection
from mote.kernel.execution.operations.output import OutputOperation


class Channel:
    def __init__(self, events):
        self.events = events

    async def project_output_candidate(self, content, candidate, *, accepted, feedback=None):
        self.events.append(("record", accepted, feedback))
        return HistoryProjection((), "history")


class Writer:
    def __init__(self, events):
        self.events = events

    async def drain(self):
        self.events.append(("drain",))


class Transaction:
    def __init__(self, events):
        self.events = events

    def context(self, operation_id):
        return operation_id

    async def stage_accepted_output(self, context, output, history):
        self.events.append(("complete",))
        self.events.append(("drain",))
        return MutationResult(MutationStatus.APPLIED)

    async def reject_output(self, context, history):
        self.events.append(("complete",))
        self.events.append(("drain",))
        self.events.append(("reap",))
        return MutationResult(MutationStatus.APPLIED)


def service(events):
    think = SimpleNamespace(result=InferenceResult(content="answer", tool_calls=None))

    async def join():
        events.append(("join",))

    think.join = join
    output_engine = SimpleNamespace(staged_output=AcceptedOutput("candidate", "contract", "1", "schema", "answer"))
    return OutputOperation(
        context=lambda: SimpleNamespace(name="agent"),
        channel=lambda: Channel(events),
        inference_engine=think,
        transaction=Transaction(events),
        output_engine=output_engine,
        report_inference_result=lambda result: events.append(("report",)),
    )


@pytest.mark.asyncio
async def test_accept_is_durable_before_inference_checkpoint_reap():
    events = []

    await service(events).accept(FinalCandidateAction(raw="answer", representation="native_text"))

    assert events == [
        ("report",),
        ("record", True, None),
        ("complete",),
        ("drain",),
        ("join",),
    ]


@pytest.mark.asyncio
async def test_rejection_feedback_is_durable_before_inference_checkpoint_reap():
    events = []
    evaluation = OutputEvaluation(
        accepted=False,
        correction_allowed=True,
        issues=(),
        correction_attempt=1,
        corrections_remaining=1,
        max_corrections=2,
    )

    await service(events).reject(
        evaluation,
        FinalCandidateAction(raw="bad", representation="native_text"),
    )

    assert events[1][0:2] == ("record", False)
    assert events[2:] == [("complete",), ("drain",), ("reap",), ("join",)]
