from __future__ import annotations

import asyncio

import pytest

from mote.contracts.conversation import UserMessage
from mote.contracts.events.conversation import MessageAppendedEvent
from mote.runtime.events.dispatcher import SubscriptionManifest
from mote.runtime.events.fabric import EventFabric
from mote.runtime.session.codec import decode_session_event
from mote.runtime.session.committer import SessionFactCommitter
from mote.runtime.session.events import MessageEvent, SessionMetaEvent, TurnContextEvent
from mote.runtime.session.log import SessionLog


async def _session_fabric(log: SessionLog) -> EventFabric:
    log.exists()
    fabric = EventFabric(
        journal=log.event_journal,
        streams=(log.stream_id,),
        subscriptions=SubscriptionManifest(()),
        on_commit=log.accept_commit,
    )
    await fabric.start()
    return fabric


@pytest.mark.asyncio
async def test_bound_session_log_routes_async_facts_through_fabric(tmp_path) -> None:
    log = SessionLog("session-1", base_dir=str(tmp_path))
    fabric = await _session_fabric(log)
    committer = SessionFactCommitter(log, fabric)
    log.bind_async_sink(committer.commit_event)

    await log.append(SessionMetaEvent(session_id="session-1", role_class="test.Role", toolset_manifest=()))
    with pytest.raises(RuntimeError, match="offline commit is forbidden"):
        log.commit_offline(MessageEvent(message=UserMessage(content="bypass")))
    await committer.commit_fact(MessageAppendedEvent(message=UserMessage(content="hello")))

    events = [decode_session_event(envelope) for envelope in log.iter_events()]
    assert isinstance(events[0], SessionMetaEvent)
    assert isinstance(events[1], MessageEvent)
    assert events[1].message.content == "hello"
    assert fabric.dispatcher.cursor(log.stream_id) == 2
    await fabric.aclose()


@pytest.mark.asyncio
async def test_synchronous_transaction_domain_commits_through_owner_loop(
    tmp_path,
) -> None:
    log = SessionLog("session-1", base_dir=str(tmp_path))
    fabric = await _session_fabric(log)
    committer = SessionFactCommitter(log, fabric)
    log.bind_async_sink(committer.commit_event)
    await log.append(SessionMetaEvent(session_id="session-1", role_class="test.Role", toolset_manifest=()))

    result = await asyncio.to_thread(
        committer.commit_event_from_thread,
        MessageEvent(message=UserMessage(content="thread")),
    )
    assert result.current_version == 2
    assert fabric.dispatcher.cursor(log.stream_id) == 2
    await log.append(TurnContextEvent(turn_id="turn-1"))

    assert fabric.dispatcher.cursor(log.stream_id) == 3
    events = [decode_session_event(envelope) for envelope in log.iter_events()]
    assert [event.type for event in events if event is not None] == [
        "session_meta",
        "message",
        "turn_context",
    ]
    await fabric.aclose()
