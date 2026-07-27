from __future__ import annotations

import asyncio
import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

import mote.runtime.events.journal as journal_module
from mote.contracts.events import EventId, EventType, StreamId
from mote.contracts.ports.event_journal import JournalIntegrityError, StreamVersionConflict, UncommittedFact
from mote.runtime.events.journal import LocalEventJournal


def _fact(
    name: str,
    *,
    payload: dict | None = None,
    event_id: str | None = None,
) -> UncommittedFact:
    return UncommittedFact(
        event_id=EventId(event_id or str(uuid4())),
        event_type=EventType(f"mote.test.{name}"),
        schema_version=1,
        occurred_at=datetime.now(timezone.utc),
        payload=payload or {"name": name},
        metadata={"source": "contract-test"},
    )


def _journal(tmp_path: Path, stream_id: StreamId) -> LocalEventJournal:
    return LocalEventJournal(tmp_path / "events.jsonl", stream_id)


async def _collect(journal: LocalEventJournal, stream_id: StreamId, after: int = 0):
    return [event async for event in journal.read(stream_id, after=after)]


def test_append_assigns_order_and_roundtrips_immutable_envelopes(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        stream_id = StreamId("session/test")
        journal = _journal(tmp_path, stream_id)
        first = _fact("first", payload={"nested": {"items": [1, 2]}})
        second = _fact("second")

        result = await journal.append(
            stream_id,
            (first, second),
            expected_version=0,
        )
        restored = await _collect(journal, stream_id)

        assert result.previous_version == 0
        assert result.current_version == 2
        assert [event.sequence for event in restored] == [1, 2]
        assert restored == list(result.envelopes)
        assert restored[0].recorded_at.tzinfo is not None
        assert restored[0].payload["nested"]["items"] == (1, 2)
        with pytest.raises(TypeError):
            restored[0].payload["new"] = True
        with pytest.raises(TypeError):
            restored[0].payload["nested"]["new"] = True
        with pytest.raises(FrozenInstanceError):
            restored[0].sequence = 9

        await journal.writer.aclose()

    asyncio.run(exercise())


def test_expected_version_is_a_compare_and_swap_boundary(tmp_path: Path) -> None:
    async def exercise() -> None:
        stream_id = StreamId("session/cas")
        journal = _journal(tmp_path, stream_id)
        await journal.append(stream_id, (_fact("one"),), expected_version=0)

        with pytest.raises(StreamVersionConflict) as raised:
            await journal.append(stream_id, (_fact("stale"),), expected_version=0)

        assert raised.value.expected == 0
        assert raised.value.actual == 1
        assert [event.event_type async for event in journal.read(stream_id)] == ["mote.test.one"]
        await journal.writer.aclose()

    asyncio.run(exercise())


def test_concurrent_appends_cannot_claim_the_same_version(tmp_path: Path) -> None:
    async def exercise() -> None:
        stream_id = StreamId("session/concurrent")
        journal = _journal(tmp_path, stream_id)
        outcomes = await asyncio.gather(
            journal.append(stream_id, (_fact("left"),), expected_version=0),
            journal.append(stream_id, (_fact("right"),), expected_version=0),
            return_exceptions=True,
        )

        assert sum(not isinstance(outcome, Exception) for outcome in outcomes) == 1
        [conflict] = [outcome for outcome in outcomes if isinstance(outcome, StreamVersionConflict)]
        assert conflict.actual == 1
        assert len(await _collect(journal, stream_id)) == 1
        await journal.writer.aclose()

    asyncio.run(exercise())


def test_append_returns_only_after_file_and_directory_fsync(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[int] = []
    real_fsync = journal_module.os.fsync

    def recording_fsync(descriptor: int) -> None:
        calls.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(journal_module.os, "fsync", recording_fsync)

    async def exercise() -> None:
        stream_id = StreamId("session/durable")
        journal = _journal(tmp_path, stream_id)
        await journal.append(
            stream_id,
            (_fact("durable"),),
            expected_version=0,
        )
        assert len(calls) == 2
        await journal.writer.aclose()

    asyncio.run(exercise())


def test_verify_and_read_reject_content_corruption(tmp_path: Path) -> None:
    async def exercise() -> None:
        stream_id = StreamId("session/corrupt")
        journal = _journal(tmp_path, stream_id)
        await journal.append(stream_id, (_fact("sound"),), expected_version=0)
        path = journal.path_for(stream_id)
        record = json.loads(path.read_text(encoding="utf-8"))
        record["envelope"]["payload"]["name"] = "tampered"
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")

        report = await journal.verify(stream_id)
        assert report.valid is False
        assert report.issues[0].code == "invalid_record"
        with pytest.raises(JournalIntegrityError):
            await _collect(journal, stream_id)
        await journal.writer.aclose()

    asyncio.run(exercise())


def test_verify_rejects_torn_record_and_append_refuses_to_extend_it(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        stream_id = StreamId("session/torn")
        journal = _journal(tmp_path, stream_id)
        await journal.append(stream_id, (_fact("complete"),), expected_version=0)
        path = journal.path_for(stream_id)
        path.write_bytes(path.read_bytes().rstrip(b"\n"))

        report = await journal.verify(stream_id)
        assert report.valid is False
        assert report.issues[0].code == "torn_record"
        with pytest.raises(JournalIntegrityError):
            await journal.append(stream_id, (_fact("after"),), expected_version=1)
        await journal.writer.aclose()

    asyncio.run(exercise())


def test_read_after_returns_a_verified_stream_tail(tmp_path: Path) -> None:
    async def exercise() -> None:
        stream_id = StreamId("session/tail")
        journal = _journal(tmp_path, stream_id)
        await journal.append(
            stream_id,
            (_fact("one"), _fact("two"), _fact("three")),
            expected_version=0,
        )

        tail = await _collect(journal, stream_id, after=1)

        assert [event.sequence for event in tail] == [2, 3]
        assert (await journal.verify(stream_id)).valid is True
        await journal.writer.aclose()

    asyncio.run(exercise())


def test_event_shape_validation_rejects_ambient_mutability(tmp_path: Path) -> None:
    async def exercise() -> None:
        stream_id = StreamId("session/shape")
        journal = _journal(tmp_path, stream_id)

        with pytest.raises(ValueError, match="namespaced"):
            await journal.append(
                stream_id,
                (
                    UncommittedFact(
                        event_id=EventId("event"),
                        event_type=EventType("unnamespaced"),
                        schema_version=1,
                        occurred_at=datetime.now(timezone.utc),
                        payload={},
                    ),
                ),
                expected_version=0,
            )
        with pytest.raises(TypeError, match="JSON-safe"):
            await journal.append(
                stream_id,
                (_fact("unsafe", payload={"bad": object()}),),
                expected_version=0,
            )
        assert not journal.path_for(stream_id).exists()
        await journal.writer.aclose()

    asyncio.run(exercise())
