from __future__ import annotations

from types import SimpleNamespace

import pytest

from mote.contracts.execution.models import MutationResult, MutationStatus
from mote.contracts.model.inference import InferenceResult
from mote.contracts.model.turn import FinalCandidateAction
from mote.contracts.output import CommittedOutput, OutputEvaluation, ValidatedCandidate
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

    async def commit_final_output(self, context, output, message):
        self.events.append(("complete",))
        self.events.append(("drain",))
        return CommittedOutput(output.candidate_id, output.contract_id, output.schema_fingerprint, output.value)

    async def reject_output(self, context, history):
        self.events.append(("complete",))
        self.events.append(("drain",))
        self.events.append(("reap",))
        return MutationResult(MutationStatus.APPLIED)


def service(events):
    think = SimpleNamespace(result=InferenceResult(content="answer", tool_calls=None), done=False)

    async def join():
        events.append(("join",))

    think.join = join
    output_engine = SimpleNamespace(
        validated_candidate=ValidatedCandidate("candidate", "contract", "schema", "answer", "answer")
    )
    return OutputOperation(
        context=lambda: SimpleNamespace(name="agent"),
        channel=lambda: Channel(events),
        inference_engine=think,
        transaction=Transaction(events),
        output_engine=output_engine,
        report_inference_result=lambda result: events.append(("report",)),
    )


@pytest.mark.asyncio
async def test_final_commit_is_durable_before_return():
    events = []

    await service(events).validate_and_commit(FinalCandidateAction(raw="answer", representation="native_text"))

    assert events == [
        ("report",),
        ("record", True, None),
        ("join",),
        ("complete",),
        ("drain",),
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
