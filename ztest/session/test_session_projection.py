from __future__ import annotations

import pytest

from mote.contracts.conversation import UserMessage
from mote.contracts.ports.events.subscription import (
    EventFilter,
    Ordering,
    OverflowPolicy,
    Reliability,
    RetryPolicy,
    SubscriptionSpec,
)
from mote.runtime.events import EventFabric, SubscriptionBinding, SubscriptionManifest
from mote.runtime.events.backends import SQLiteSubscriptionStateStore
from mote.runtime.projections.session import (
    SESSION_PROJECTION_SUBSCRIPTION,
    ContextCompactionSourceError,
    SessionLiveProjection,
    SessionProjectionSequenceError,
    SessionProjectionState,
    reduce_session_envelope,
)
from mote.runtime.session.events import ContextCompactedFact, MessageEvent, RoutingDecisionFact, SessionMetaEvent
from mote.runtime.session.log import SessionLog
from mote.runtime.session.replay import replay


def _log(tmp_path) -> SessionLog:
    log = SessionLog("projection", base_dir=str(tmp_path))
    log.commit_offline(SessionMetaEvent(session_id="projection"))
    return log


def _projection_manifest(
    log: SessionLog,
    projection: SessionLiveProjection,
) -> SubscriptionManifest:
    return SubscriptionManifest(
        (
            SubscriptionBinding(
                SubscriptionSpec(
                    identity=SESSION_PROJECTION_SUBSCRIPTION,
                    event_filter=EventFilter(stream_prefixes=(str(log.stream_id),)),
                    reliability=Reliability.DURABLE,
                    ordering=Ordering.PER_STREAM,
                    capacity=4,
                    overflow=OverflowPolicy.BACKPRESSURE,
                    retry=RetryPolicy(
                        max_attempts=1,
                        initial_delay_seconds=0,
                        maximum_delay_seconds=0,
                        jitter_ratio=0,
                    ),
                ),
                projection,
            ),
        )
    )


def test_incremental_and_replay_reduction_are_identical(tmp_path) -> None:
    log = _log(tmp_path)
    old = UserMessage(content="old")
    log.commit_offline(MessageEvent(old))
    log.commit_offline(
        ContextCompactedFact(
            model_context_messages=[UserMessage(content="summary")],
            source_message_ids=[str(old.id)],
            summary="summary",
        )
    )
    log.commit_offline(MessageEvent(UserMessage(content="tail")))
    live = SessionProjectionState()

    for envelope in log.iter_events():
        reduce_session_envelope(live, envelope)

    rebuilt = replay(log)
    assert live.through_sequence == rebuilt.through_sequence
    assert live.meta == rebuilt.meta
    assert live.transcript_messages == rebuilt.transcript_messages
    assert live.model_context_messages == rebuilt.model_context_messages
    assert live.message_events == rebuilt.message_events
    assert live.compactions == rebuilt.compactions


def test_replay_restores_latest_routing_state(tmp_path) -> None:
    log = _log(tmp_path)
    log.commit_offline(
        RoutingDecisionFact(
            decision={
                "decision_id": "decision-1",
                "selected_route_id": "strong",
                "state_generation": 1,
            },
            state={
                "schema_version": 1,
                "generation": 1,
                "recent_decisions": [
                    {
                        "decision_id": "decision-1",
                        "selected_route_id": "strong",
                        "final_class": "R3",
                        "turn_id": 2,
                    }
                ],
                "seed_floor": None,
                "control_hold": None,
            },
        )
    )
    restored = replay(log)
    assert restored.routing_state.generation == 1
    assert restored.routing_state.recent_decisions[-1].final_class == "R3"


def test_duplicate_delivery_does_not_reapply_a_fact(tmp_path) -> None:
    log = _log(tmp_path)
    log.commit_offline(MessageEvent(UserMessage(content="once")))
    metadata, message = tuple(log.iter_events())
    state = SessionProjectionState()

    assert reduce_session_envelope(state, metadata) is True
    assert reduce_session_envelope(state, message) is True
    assert reduce_session_envelope(state, message) is False
    assert [item.content for item in state.transcript_messages] == ["once"]
    assert [item.content for item in state.model_context_messages] == ["once"]


def test_projection_rejects_a_sequence_gap(tmp_path) -> None:
    log = _log(tmp_path)
    log.commit_offline(MessageEvent(UserMessage(content="gap")))
    _, second = tuple(log.iter_events())

    with pytest.raises(SessionProjectionSequenceError, match="expected sequence 1"):
        reduce_session_envelope(SessionProjectionState(), second)


def test_projection_rejects_compaction_of_a_stale_context_revision(tmp_path) -> None:
    log = _log(tmp_path)
    log.commit_offline(MessageEvent(UserMessage(content="current")))
    log.commit_offline(
        ContextCompactedFact(
            model_context_messages=[UserMessage(content="summary")],
            source_message_ids=["stale-id"],
            summary="summary",
        )
    )
    state = SessionProjectionState()
    metadata, message, compaction = tuple(log.iter_events())
    reduce_session_envelope(state, metadata)
    reduce_session_envelope(state, message)

    with pytest.raises(ContextCompactionSourceError, match="source does not match"):
        reduce_session_envelope(state, compaction)


@pytest.mark.asyncio
async def test_live_projection_restores_then_reduces_new_commits(tmp_path) -> None:
    log = _log(tmp_path)
    first = UserMessage(content="first")
    log.commit_offline(MessageEvent(first))
    projection = SessionLiveProjection(log.stream_id)

    projection.restore(log.iter_events())
    restored = projection.snapshot()
    assert restored.through_sequence == 2
    assert [(message.id, message.content) for message in restored.transcript_messages] == [(first.id, "first")]

    second = UserMessage(content="second")
    result = log.commit_offline(MessageEvent(second))
    await projection.handle(result.envelopes[0])

    current = projection.snapshot()
    assert current.through_sequence == 3
    assert [message.content for message in current.transcript_messages] == [
        "first",
        "second",
    ]
    current.transcript_messages.clear()
    assert [message.content for message in projection.snapshot().transcript_messages] == ["first", "second"]


@pytest.mark.asyncio
async def test_live_projection_rejects_another_session_stream(tmp_path) -> None:
    source = _log(tmp_path / "source")
    other = SessionLog("other", base_dir=str(tmp_path / "other"))
    other.commit_offline(SessionMetaEvent(session_id="other"))
    projection = SessionLiveProjection(source.stream_id)

    with pytest.raises(ValueError, match="another stream"):
        await projection.handle(tuple(other.iter_events())[0])


@pytest.mark.asyncio
async def test_durable_checkpoint_never_replaces_startup_projection_rebuild(
    tmp_path,
) -> None:
    log = _log(tmp_path)
    message = UserMessage(content="survives restart")
    committed = log.commit_offline(MessageEvent(message))
    state_path = tmp_path / "subscription-state.sqlite3"

    first_projection = SessionLiveProjection(log.stream_id)
    first_projection.restore(log.iter_events())
    first_store = SQLiteSubscriptionStateStore(state_path)
    first_fabric = EventFabric(
        journal=log.event_journal,
        streams=(log.stream_id,),
        subscriptions=_projection_manifest(log, first_projection),
        state_store=first_store,
    )
    await first_fabric.start()
    try:
        await first_fabric.wait_until(
            SESSION_PROJECTION_SUBSCRIPTION,
            log.stream_id,
            committed.last_sequence,
        )
    finally:
        await first_fabric.aclose()

    restarted_projection = SessionLiveProjection(log.stream_id)
    restarted_projection.restore(log.iter_events())
    restarted_store = SQLiteSubscriptionStateStore(state_path)
    restarted_fabric = EventFabric(
        journal=log.event_journal,
        streams=(log.stream_id,),
        subscriptions=_projection_manifest(log, restarted_projection),
        state_store=restarted_store,
    )
    await restarted_fabric.start()
    try:
        state = restarted_projection.snapshot()
        assert state.through_sequence == committed.last_sequence
        assert [item.content for item in state.transcript_messages] == ["survives restart"]
        assert (
            await restarted_store.load(
                SESSION_PROJECTION_SUBSCRIPTION,
                log.stream_id,
            )
            == committed.last_sequence
        )
    finally:
        await restarted_fabric.aclose()
