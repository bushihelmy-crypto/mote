from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from mote.contracts.events.envelope import EventId, EventType, StreamId
from mote.contracts.ports.events.journal import (
    StreamVersionConflict,
    StreamWriterFence,
    StreamWriterFenced,
    UncommittedFact,
)
from mote.runtime.events.dispatcher import SubscriptionManifest
from mote.runtime.events.fabric import EventFabric
from mote.runtime.events.journal import LocalEventJournal

_STREAM = StreamId("session/guarded")


def _fact(name: str) -> UncommittedFact:
    return UncommittedFact(
        EventId(f"event-{name}"),
        EventType("mote.test.guarded"),
        1,
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        {"name": name},
    )


class _WriterGuard:
    def __init__(self) -> None:
        self.current = StreamWriterFence("run-1", "worker-1", "incarnation-1", 1)

    @contextmanager
    def guard_append(self, writer: StreamWriterFence):
        if writer != self.current:
            raise StreamWriterFenced("stale writer")
        yield


@pytest.mark.asyncio
async def test_guarded_append_does_not_retry_a_stale_stream_version(tmp_path) -> None:
    guard = _WriterGuard()
    journal = LocalEventJournal(
        tmp_path / "events.jsonl",
        _STREAM,
        guarded_append_authority=guard,
    )
    fabric = EventFabric(journal=journal, streams=(_STREAM,), subscriptions=SubscriptionManifest(()))
    await fabric.start()
    await fabric.append_guarded(_STREAM, (_fact("one"),), expected_version=0, writer=guard.current)

    with pytest.raises(StreamVersionConflict):
        await fabric.append_guarded(_STREAM, (_fact("stale"),), expected_version=0, writer=guard.current)

    assert (await journal.verify(_STREAM)).current_version == 1
    await fabric.aclose()


@pytest.mark.asyncio
async def test_guarded_append_rejects_stale_writer_without_a_write(tmp_path) -> None:
    guard = _WriterGuard()
    journal = LocalEventJournal(
        tmp_path / "events.jsonl",
        _STREAM,
        guarded_append_authority=guard,
    )
    stale = StreamWriterFence("run-1", "worker-old", "incarnation-old", 1)

    with pytest.raises(StreamWriterFenced):
        await journal.append_guarded(_STREAM, (_fact("stale"),), expected_version=0, writer=stale)

    assert (await journal.verify(_STREAM)).current_version == 0
    await journal.writer.aclose()


@pytest.mark.asyncio
async def test_guarded_append_requires_an_atomic_writer_guard(tmp_path) -> None:
    journal = LocalEventJournal(tmp_path / "events.jsonl", _STREAM)
    writer = StreamWriterFence("run-1", "worker-1", "incarnation-1", 1)

    with pytest.raises(RuntimeError, match="no guarded append authority"):
        await journal.append_guarded(_STREAM, (_fact("one"),), expected_version=0, writer=writer)
