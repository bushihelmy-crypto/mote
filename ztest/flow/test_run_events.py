"""Public RunEvent stream contracts."""

from __future__ import annotations

from dataclasses import asdict

import pytest

from mote.contracts.schema import UserMessage
from mote.kernel.flow import RunFailed, RunPhase, RunPhaseStarted, RunStarted, RunSucceeded
from mote.kernel.flow.graph import build_review_refine_graph

from .conftest import FakeChannel, FakeThinkEngine

pytestmark = pytest.mark.asyncio


def _news(bundle):
    bundle.buffer.push(UserMessage("go", send_to={"Alice"}))


async def test_run_events_are_typed_ordered_and_carry_the_result(make_engine):
    bundle = make_engine(
        think_engine=FakeThinkEngine(content="done"),
        channel=FakeChannel(terminal=True),
    )
    _news(bundle)

    events = [event async for event in bundle.engine.run_events()]

    assert isinstance(events[0], RunStarted)
    assert isinstance(events[-1], RunSucceeded)
    assert events[-1].result.presentation.content == "done"
    assert {event.run_id for event in events} == {events[0].run_id}
    phases = [event.phase for event in events if isinstance(event, RunPhaseStarted)]
    assert phases == [
        RunPhase.RECOVERY,
        RunPhase.OBSERVATION,
        RunPhase.BUDGET,
        RunPhase.MODEL,
        RunPhase.INTERPRETATION,
        RunPhase.OUTPUT,
    ]


async def test_run_events_do_not_expose_graph_or_node_identifiers(make_engine):
    bundle = make_engine(
        think_engine=FakeThinkEngine(content="done"),
        channel=FakeChannel(terminal=True),
        graph_builder=build_review_refine_graph,
    )
    _news(bundle)

    events = [event async for event in bundle.engine.run_events()]

    assert all("node" not in asdict(event) and "graph" not in asdict(event) for event in events)
    assert RunPhase.ACTION not in [event.phase for event in events if isinstance(event, RunPhaseStarted)]


async def test_run_events_ends_with_typed_failure_without_raising_to_consumer(make_engine):
    bundle = make_engine(
        channel=FakeChannel(commands=[{"id": "t1", "command_name": "Read", "args": {}}]),
        graph_builder=build_review_refine_graph,
    )
    _news(bundle)

    events = [event async for event in bundle.engine.run_events()]

    assert isinstance(events[-1], RunFailed)
    assert events[-1].error_type == "RuntimeError"
    assert "tool actions are disabled" in events[-1].message
