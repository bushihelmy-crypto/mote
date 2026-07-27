from __future__ import annotations

from types import SimpleNamespace

import pytest

from mote.contracts.model_actions import FinalCandidateAction
from mote.contracts.output import OutputEvaluation
from mote.contracts.think import ThinkResult
from mote.kernel.flow.services.output import FlowOutputService


class Channel:
    def __init__(self, events):
        self.events = events

    async def record_output_candidate(self, memory, content, candidate, *, accepted, feedback=None):
        self.events.append(("record", accepted, feedback))


class Writer:
    def __init__(self, events):
        self.events = events

    async def drain(self):
        self.events.append(("drain",))


def service(events):
    think = SimpleNamespace(result=ThinkResult(content="answer", tool_calls=None))

    async def join():
        events.append(("join",))

    think.join = join
    return FlowOutputService(
        context=lambda: SimpleNamespace(name="agent"),
        channel=lambda: Channel(events),
        think_engine=think,
        memory=SimpleNamespace(),
        output_engine=SimpleNamespace(),
        report_think_result=lambda result: events.append(("report",)),
        complete_think=lambda: events.append(("complete",)),
        reap_think=lambda: events.append(("reap",)),
        drain_writes=Writer(events).drain,
    )


@pytest.mark.asyncio
async def test_accept_is_durable_before_think_checkpoint_reap():
    events = []

    await service(events).accept(FinalCandidateAction(raw="answer", representation="native_text"))

    assert events == [
        ("report",),
        ("complete",),
        ("record", True, None),
        ("drain",),
        ("reap",),
        ("join",),
    ]


@pytest.mark.asyncio
async def test_rejection_feedback_is_durable_before_think_checkpoint_reap():
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

    assert events[2][0:2] == ("record", False)
    assert events[3:] == [("drain",), ("reap",), ("join",)]
