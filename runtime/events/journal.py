"""Single-process, crash-safe local implementation of the EventJournal port."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, ContextManager, Iterator, Mapping, Optional, Protocol, Sequence, cast

from mote.contracts.events.envelope import (
    CorrelationId,
    EventEnvelope,
    EventId,
    EventType,
    JsonValue,
    StreamId,
    thaw_json,
)
from mote.contracts.ports.events.journal import (
    AppendResult,
    JournalIntegrityError,
    StreamVersionConflict,
    UncommittedFact,
    VerificationIssue,
    VerificationReport,
)
from mote.runtime.persistence.async_io import run_disk_io
from mote.runtime.persistence.writer import DiskWriter
from mote.runtime.telemetry.logging import log_class

_STORAGE_FORMAT_VERSION = 1
_MAX_RECORD_BYTES = 64 * 1024 * 1024
_MAX_BATCH_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class _StreamState:
    version: int
    last_checksum: Optional[str]
    byte_size: int


@dataclass(frozen=True)
class _ScanResult:
    state: _StreamState
    report: VerificationReport


class JournalCommitGuard(Protocol):
    def guard(self) -> ContextManager[None]: ...


@log_class(level="DEBUG", exclude={"path_for"})
class LocalEventJournal:
    """Append-only JSONL journal owned by one Runtime process.

    Process admission is intentionally outside this class. The Event Fabric is
    process-local; this backend serializes appenders with an owned async lock
    and treats any out-of-band file mutation as integrity loss.
    """

    def __init__(
        self,
        path: str | Path,
        stream_id: StreamId,
        *,
        writer: DiskWriter | None = None,
        commit_guard: JournalCommitGuard | None = None,
    ) -> None:
        self._path = Path(path)
        self._stream_id = StreamId(_validate_stream_id(stream_id))
        self._writer = writer or DiskWriter()
        self._states: dict[str, _StreamState] = {}
        self._commit_lock = threading.Lock()
        self._commit_guard = commit_guard

    @property
    def writer(self) -> DiskWriter:
        return self._writer

    def path_for(self, stream_id: StreamId) -> Path:
        stream = _validate_stream_id(stream_id)
        if stream != self._stream_id:
            raise ValueError(f"journal is bound to stream {self._stream_id!r}, not {stream!r}")
        return self._path

    async def append(
        self,
        stream_id: StreamId,
        facts: Sequence[UncommittedFact],
        *,
        expected_version: int,
    ) -> AppendResult:
        return await run_disk_io(
            self.append_committed,
            stream_id,
            facts,
            expected_version=expected_version,
        )

    def append_committed(
        self,
        stream_id: StreamId,
        facts: Sequence[UncommittedFact],
        *,
        expected_version: int,
    ) -> AppendResult:
        """Synchronously commit through the same CAS/fsync implementation."""

        stream = StreamId(_validate_stream_id(stream_id))
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("expected_version must be a non-negative integer")
        batch = tuple(facts)
        if not batch:
            raise ValueError("append requires at least one fact")
        if any(not isinstance(fact, UncommittedFact) for fact in batch):
            raise TypeError("facts must contain only UncommittedFact values")
        event_ids = [fact.event_id for fact in batch]
        if len(set(event_ids)) != len(event_ids):
            raise ValueError("one append cannot contain duplicate event IDs")
        self.path_for(stream)
        with self._commit_lock:
            guard = self._commit_guard.guard() if self._commit_guard is not None else nullcontext()
            with guard:
                return self._append_sync(
                    stream,
                    batch,
                    expected_version=expected_version,
                )

    async def read(
        self,
        stream_id: StreamId,
        *,
        after: int = 0,
    ) -> AsyncIterator[EventEnvelope[Mapping[str, JsonValue]]]:
        stream = StreamId(_validate_stream_id(stream_id))
        if type(after) is not int or after < 0:
            raise ValueError("after must be a non-negative integer")
        path = self.path_for(stream)
        snapshot_size = await run_disk_io(
            self._committed_snapshot_size,
            stream,
            path,
        )
        for envelope in _iter_envelopes(path, stream, snapshot_size):
            if envelope.sequence > after:
                yield envelope

    async def verify(self, stream_id: StreamId) -> VerificationReport:
        stream = StreamId(_validate_stream_id(stream_id))
        return await run_disk_io(self.verify_committed, stream)

    def iter_committed(
        self,
        stream_id: StreamId,
        *,
        after: int = 0,
    ) -> Iterator[EventEnvelope[Mapping[str, JsonValue]]]:
        """Synchronously read a verified snapshot during recovery/query paths."""

        stream = StreamId(_validate_stream_id(stream_id))
        if type(after) is not int or after < 0:
            raise ValueError("after must be a non-negative integer")
        self._writer.flush_inline()
        path = self.path_for(stream)
        snapshot_size = self._committed_snapshot_size(stream, path)
        return (envelope for envelope in _iter_envelopes(path, stream, snapshot_size) if envelope.sequence > after)

    def verify_committed(self, stream_id: StreamId) -> VerificationReport:
        """Synchronously verify a stream before the Runtime event loop starts."""

        stream = StreamId(_validate_stream_id(stream_id))
        self._writer.flush_inline()
        with self._commit_lock:
            scan = _scan(self.path_for(stream), stream)
            if scan.report.valid:
                self._states[str(stream)] = scan.state
            else:
                self._states.pop(str(stream), None)
            return scan.report

    def _append_sync(
        self,
        stream_id: StreamId,
        facts: tuple[UncommittedFact, ...],
        *,
        expected_version: int,
    ) -> AppendResult:
        path = self.path_for(stream_id)
        state = self._load_state(stream_id, path)
        if state.version != expected_version:
            raise StreamVersionConflict(stream_id, expected_version, state.version)
        actual_size = path.stat().st_size if path.exists() else 0
        if actual_size != state.byte_size:
            raise JournalIntegrityError(f"stream {stream_id!r} changed outside its owning EventJournal")

        recorded_at = datetime.now(timezone.utc)
        previous_checksum = state.last_checksum
        envelopes: list[EventEnvelope[Mapping[str, JsonValue]]] = []
        lines: list[bytes] = []
        for offset, fact in enumerate(facts, start=1):
            envelope = EventEnvelope(
                event_id=fact.event_id,
                event_type=fact.event_type,
                schema_version=fact.schema_version,
                stream_id=stream_id,
                sequence=state.version + offset,
                occurred_at=fact.occurred_at,
                recorded_at=recorded_at,
                payload=fact.payload,
                session_id=fact.session_id,
                run_id=fact.run_id,
                turn_id=fact.turn_id,
                correlation_id=fact.correlation_id,
                causation_id=fact.causation_id,
                trace_id=fact.trace_id,
                span_id=fact.span_id,
                metadata=fact.metadata,
            )
            line, previous_checksum = encode_event_record(envelope, previous_checksum)
            if len(line) > _MAX_RECORD_BYTES:
                raise ValueError(f"event {fact.event_id!r} exceeds the journal record bound")
            lines.append(line)
            envelopes.append(envelope)

        data = b"".join(lines)
        if len(data) > _MAX_BATCH_BYTES:
            raise ValueError("event batch exceeds the journal append bound")
        path.parent.mkdir(parents=True, exist_ok=True)
        created = not path.exists()
        with path.open("ab") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if created:
            _fsync_directory(path.parent)

        current = _StreamState(
            version=state.version + len(envelopes),
            last_checksum=previous_checksum,
            byte_size=state.byte_size + len(data),
        )
        self._states[str(stream_id)] = current
        return AppendResult(
            stream_id=stream_id,
            previous_version=state.version,
            current_version=current.version,
            envelopes=tuple(envelopes),
        )

    def _load_state(self, stream_id: StreamId, path: Path) -> _StreamState:
        cached = self._states.get(str(stream_id))
        if cached is not None:
            return cached
        scan = _scan(path, stream_id)
        if not scan.report.valid:
            raise JournalIntegrityError(_format_issues(stream_id, scan.report.issues))
        self._states[str(stream_id)] = scan.state
        return scan.state

    def _verified_snapshot_size(self, stream_id: StreamId, path: Path) -> int:
        scan = _scan(path, stream_id)
        if not scan.report.valid:
            raise JournalIntegrityError(_format_issues(stream_id, scan.report.issues))
        self._states[str(stream_id)] = scan.state
        return scan.state.byte_size

    def _committed_snapshot_size(self, stream_id: StreamId, path: Path) -> int:
        with self._commit_lock:
            return self._verified_snapshot_size(stream_id, path)


def _validate_stream_id(stream_id: object) -> str:
    if type(stream_id) is not str or not stream_id:
        raise ValueError("stream_id must be a non-empty string")
    encoded = stream_id.encode("utf-8")
    if len(encoded) > 512:
        raise ValueError("stream_id exceeds its 512-byte bound")
    if any(ord(char) < 32 for char in stream_id):
        raise ValueError("stream_id contains a control character")
    return stream_id


def _envelope_record(
    envelope: EventEnvelope[Mapping[str, JsonValue]],
) -> dict[str, object]:
    return {
        "event_id": str(envelope.event_id),
        "event_type": str(envelope.event_type),
        "schema_version": envelope.schema_version,
        "stream_id": str(envelope.stream_id),
        "sequence": envelope.sequence,
        "occurred_at": envelope.occurred_at.isoformat(),
        "recorded_at": envelope.recorded_at.isoformat(),
        "session_id": envelope.session_id,
        "run_id": envelope.run_id,
        "turn_id": envelope.turn_id,
        "correlation_id": envelope.correlation_id,
        "causation_id": envelope.causation_id,
        "trace_id": envelope.trace_id,
        "span_id": envelope.span_id,
        "payload": thaw_json(cast(JsonValue, envelope.payload)),
        "metadata": thaw_json(cast(JsonValue, envelope.metadata)),
    }


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _record_checksum(envelope: Mapping[str, object], previous: Optional[str]) -> str:
    digest = hashlib.sha256()
    if previous is not None:
        digest.update(previous.encode("ascii"))
    digest.update(b"\0")
    digest.update(_canonical_json(envelope))
    return digest.hexdigest()


def encode_event_record(
    envelope: EventEnvelope[Mapping[str, JsonValue]],
    previous_checksum: Optional[str],
) -> tuple[bytes, str]:
    body = _envelope_record(envelope)
    checksum = _record_checksum(body, previous_checksum)
    record = {
        "format_version": _STORAGE_FORMAT_VERSION,
        "previous_checksum": previous_checksum,
        "checksum": checksum,
        "envelope": body,
    }
    return _canonical_json(record) + b"\n", checksum


def _decode_record(
    value: object,
) -> tuple[EventEnvelope[Mapping[str, JsonValue]], Optional[str], str, dict]:
    if type(value) is not dict or set(value) != {
        "format_version",
        "previous_checksum",
        "checksum",
        "envelope",
    }:
        raise ValueError("record shape is invalid")
    if value["format_version"] != _STORAGE_FORMAT_VERSION:
        raise ValueError("storage format version is unsupported")
    previous = value["previous_checksum"]
    checksum = value["checksum"]
    body = value["envelope"]
    if previous is not None and not _is_checksum(previous):
        raise ValueError("previous checksum is invalid")
    if not _is_checksum(checksum):
        raise ValueError("checksum is invalid")
    if type(body) is not dict or set(body) != {
        "event_id",
        "event_type",
        "schema_version",
        "stream_id",
        "sequence",
        "occurred_at",
        "recorded_at",
        "session_id",
        "run_id",
        "turn_id",
        "correlation_id",
        "causation_id",
        "trace_id",
        "span_id",
        "payload",
        "metadata",
    }:
        raise ValueError("envelope shape is invalid")
    if type(body["payload"]) is not dict:
        raise ValueError("fact payload must be a JSON object")
    if type(body["metadata"]) is not dict:
        raise ValueError("fact metadata must be a JSON object")
    envelope: EventEnvelope[Mapping[str, JsonValue]] = EventEnvelope(
        event_id=EventId(body["event_id"]),
        event_type=EventType(body["event_type"]),
        schema_version=body["schema_version"],
        stream_id=StreamId(body["stream_id"]),
        sequence=body["sequence"],
        occurred_at=datetime.fromisoformat(body["occurred_at"]),
        recorded_at=datetime.fromisoformat(body["recorded_at"]),
        payload=cast(Mapping[str, JsonValue], body["payload"]),
        session_id=body["session_id"],
        run_id=body["run_id"],
        turn_id=body["turn_id"],
        correlation_id=(CorrelationId(body["correlation_id"]) if body["correlation_id"] is not None else None),
        causation_id=(EventId(body["causation_id"]) if body["causation_id"] is not None else None),
        trace_id=body["trace_id"],
        span_id=body["span_id"],
        metadata=cast(Mapping[str, JsonValue], body["metadata"]),
    )
    return envelope, previous, checksum, body


def decode_event_record(
    raw: bytes | str,
) -> EventEnvelope[Mapping[str, JsonValue]]:
    """Decode and checksum-verify one complete storage record in isolation."""

    value = json.loads(raw)
    envelope, previous, checksum, body = _decode_record(value)
    if _record_checksum(body, previous) != checksum:
        raise ValueError("record checksum does not match its content")
    return envelope


def _scan(path: Path, stream_id: StreamId) -> _ScanResult:
    if not path.exists():
        state = _StreamState(version=0, last_checksum=None, byte_size=0)
        return _ScanResult(
            state=state,
            report=VerificationReport(
                stream_id=stream_id,
                valid=True,
                record_count=0,
                current_version=0,
                last_checksum=None,
            ),
        )
    byte_size = path.stat().st_size
    sequence = 0
    previous_checksum: Optional[str] = None
    seen_event_ids: set[EventId] = set()
    issues: list[VerificationIssue] = []
    with path.open("rb") as stream:
        line_number = 0
        while True:
            raw = stream.readline(_MAX_RECORD_BYTES + 1)
            if not raw:
                break
            line_number += 1
            if len(raw) > _MAX_RECORD_BYTES:
                issues.append(VerificationIssue(line_number, "record_too_large", "record exceeds its byte bound"))
                break
            if not raw.endswith(b"\n"):
                issues.append(VerificationIssue(line_number, "torn_record", "record has no terminating newline"))
                break
            try:
                value = json.loads(raw)
                envelope, linked_checksum, checksum, body = _decode_record(value)
                if envelope.stream_id != stream_id:
                    raise ValueError("envelope belongs to a different stream")
                if envelope.sequence != sequence + 1:
                    raise ValueError("stream sequence is not contiguous")
                if linked_checksum != previous_checksum:
                    raise ValueError("checksum chain does not link to its predecessor")
                if _record_checksum(body, linked_checksum) != checksum:
                    raise ValueError("record checksum does not match its content")
                if envelope.event_id in seen_event_ids:
                    raise ValueError("event ID is duplicated within the stream")
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                issues.append(VerificationIssue(line_number, "invalid_record", str(exc)))
                break
            sequence = envelope.sequence
            previous_checksum = checksum
            seen_event_ids.add(envelope.event_id)

    state = _StreamState(
        version=sequence,
        last_checksum=previous_checksum,
        byte_size=byte_size,
    )
    return _ScanResult(
        state=state,
        report=VerificationReport(
            stream_id=stream_id,
            valid=not issues,
            record_count=sequence,
            current_version=sequence,
            last_checksum=previous_checksum,
            issues=tuple(issues),
        ),
    )


def _iter_envelopes(
    path: Path,
    stream_id: StreamId,
    snapshot_size: int,
) -> Iterator[EventEnvelope[Mapping[str, JsonValue]]]:
    if snapshot_size == 0:
        return
    consumed = 0
    with path.open("rb") as stream:
        while consumed < snapshot_size:
            raw = stream.readline(min(_MAX_RECORD_BYTES + 1, snapshot_size - consumed))
            if not raw:
                raise JournalIntegrityError(f"verified stream {stream_id!r} became shorter during read")
            consumed += len(raw)
            try:
                envelope, _, _, _ = _decode_record(json.loads(raw))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise JournalIntegrityError(f"verified stream {stream_id!r} changed during read") from exc
            yield envelope
    if consumed != snapshot_size:
        raise JournalIntegrityError(f"verified stream {stream_id!r} crossed its snapshot boundary")


def _is_checksum(value: object) -> bool:
    return type(value) is str and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _format_issues(
    stream_id: StreamId,
    issues: tuple[VerificationIssue, ...],
) -> str:
    detail = "; ".join(f"line {issue.line} {issue.code}: {issue.detail}" for issue in issues)
    return f"stream {stream_id!r} failed journal verification: {detail}"


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "JournalCommitGuard",
    "LocalEventJournal",
    "decode_event_record",
    "encode_event_record",
]
