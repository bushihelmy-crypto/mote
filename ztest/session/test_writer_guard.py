from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mote.contracts.events.envelope import EventId, EventType, StreamId
from mote.contracts.ports.events.journal import StreamWriterFenced, UncommittedFact
from mote.runtime.control.leases import FileLeaseCoordinator
from mote.runtime.events.journal import LocalEventJournal
from mote.runtime.session.writer_guard import SessionRunWriterGuard


def _fact(name: str) -> UncommittedFact:
    return UncommittedFact(
        EventId(name),
        EventType("mote.test.writer"),
        1,
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        {"name": name},
    )


@pytest.mark.asyncio
async def test_run_takeover_fences_old_session_append(tmp_path) -> None:
    coordinator = FileLeaseCoordinator(tmp_path / "writers.json")
    first = SessionRunWriterGuard(
        coordinator,
        session_id="session-1",
        owner_id="worker-1",
        incarnation_id="incarnation-1",
    )
    writer = first.acquire_run("run-1")
    stream = StreamId("session/session-1")
    journal = LocalEventJournal(
        tmp_path / "rollout.jsonl",
        stream,
        guarded_append_authority=first,
    )
    assert (
        await journal.append_guarded(stream, (_fact("one"),), expected_version=0, writer=writer)
    ).current_version == 1

    first.release_run(writer)
    replacement = SessionRunWriterGuard(
        coordinator,
        session_id="session-1",
        owner_id="worker-2",
        incarnation_id="incarnation-2",
    )
    replacement.acquire_run("run-1")
    with pytest.raises(StreamWriterFenced):
        await journal.append_guarded(stream, (_fact("stale"),), expected_version=1, writer=writer)

    assert (await journal.verify(stream)).current_version == 1
    await journal.writer.aclose()


@pytest.mark.asyncio
async def test_session_writer_lifecycle_uses_the_same_coordinator(tmp_path) -> None:
    coordinator = FileLeaseCoordinator(tmp_path / "writers.json")
    guard = SessionRunWriterGuard(
        coordinator,
        session_id="session-1",
        owner_id="worker-1",
        incarnation_id="incarnation-1",
    )

    await guard.start()
    assert guard.lifecycle_generation == 1
    with guard.guard():
        pass
    await guard.aclose()

    replacement = SessionRunWriterGuard(
        coordinator,
        session_id="session-1",
        owner_id="worker-2",
        incarnation_id="incarnation-2",
    )
    await replacement.start()
    assert replacement.lifecycle_generation == 2
    await replacement.aclose()
