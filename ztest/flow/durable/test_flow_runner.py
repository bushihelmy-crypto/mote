from __future__ import annotations

import asyncio

import pytest

from mote.kernel.flow.graph import AgentGraph, EffectKind, End, NodeId
from mote.kernel.flow.recovery import DurableFlowRunner, RecoveryDirective


class Node:
    node_id = NodeId.RESTORE
    effect_kind = EffectKind.PURE
    allowed_targets = frozenset()

    def __init__(self, outcome=None, error: BaseException | None = None, *, effect=EffectKind.PURE):
        self.outcome = outcome
        self.error = error
        self.effect_kind = effect

    async def run(self, state):
        if self.error is not None:
            raise self.error
        return End(self.outcome)


def graph(node):
    return AgentGraph(start=NodeId.RESTORE, nodes={NodeId.RESTORE: node})


@pytest.mark.asyncio
async def test_success_does_not_resolve_failure_hooks():
    cancelled = []
    failed = []
    runner = DurableFlowRunner(
        graph(Node("ok")),
        on_cancel=lambda: cancelled.append(True),
        on_failure=lambda: failed.append(True),
    )

    assert await runner.run(object()) == "ok"
    assert cancelled == []
    assert failed == []


@pytest.mark.asyncio
async def test_cancellation_is_abandoned_not_failed():
    cancelled = []
    failed = []
    runner = DurableFlowRunner(
        graph(Node(error=asyncio.CancelledError())),
        on_cancel=lambda: cancelled.append(True),
        on_failure=lambda: failed.append(True),
    )

    with pytest.raises(asyncio.CancelledError):
        await runner.run(object())
    assert cancelled == [True]
    assert failed == []


@pytest.mark.asyncio
async def test_exception_is_failed_not_abandoned():
    cancelled = []
    failed = []
    runner = DurableFlowRunner(
        graph(Node(error=RuntimeError("boom"))),
        on_cancel=lambda: cancelled.append(True),
        on_failure=lambda: failed.append(True),
    )

    with pytest.raises(RuntimeError, match="boom"):
        await runner.run(object())
    assert cancelled == []
    assert failed == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("effect", "directive"),
    [
        (EffectKind.PURE, RecoveryDirective.RESTART),
        (EffectKind.REPLAYABLE, RecoveryDirective.REINSTATE),
        (EffectKind.LEDGERED, RecoveryDirective.RECONCILE),
        (EffectKind.EXTERNAL, RecoveryDirective.RECONCILE),
        (EffectKind.WAITABLE, RecoveryDirective.RESUME_WAIT),
    ],
)
async def test_effect_kind_drives_node_recovery_contract(effect, directive):
    events = []
    runner = DurableFlowRunner(
        graph(Node("ok", effect=effect)),
        on_cancel=lambda: None,
        on_failure=lambda: None,
        on_node_started=lambda attempt: events.append(("started", attempt)),
        on_node_completed=lambda attempt: events.append(("completed", attempt)),
    )

    assert await runner.run(object()) == "ok"
    assert [event for event, _attempt in events] == ["started", "completed"]
    assert all(attempt.effect_kind is effect for _event, attempt in events)
    assert all(attempt.recovery is directive for _event, attempt in events)


@pytest.mark.asyncio
async def test_external_failure_requires_reconcile_and_is_never_retried():
    attempts = []
    failed_attempts = []
    node = Node(error=RuntimeError("unknown after effect"), effect=EffectKind.EXTERNAL)
    original_run = node.run

    async def counted_run(state):
        attempts.append(True)
        return await original_run(state)

    node.run = counted_run
    runner = DurableFlowRunner(
        graph(node),
        on_cancel=lambda: None,
        on_failure=lambda: None,
        on_node_failed=failed_attempts.append,
    )

    with pytest.raises(RuntimeError, match="unknown after effect"):
        await runner.run(object())
    assert attempts == [True]
    assert failed_attempts[0].recovery is RecoveryDirective.RECONCILE


@pytest.mark.asyncio
async def test_cancelled_wait_is_abandoned_with_resume_wait_contract():
    abandoned = []
    runner = DurableFlowRunner(
        graph(Node(error=asyncio.CancelledError(), effect=EffectKind.WAITABLE)),
        on_cancel=lambda: None,
        on_failure=lambda: None,
        on_node_abandoned=abandoned.append,
    )

    with pytest.raises(asyncio.CancelledError):
        await runner.run(object())
    assert abandoned[0].recovery is RecoveryDirective.RESUME_WAIT
