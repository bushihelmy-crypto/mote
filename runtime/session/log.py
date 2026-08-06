"""SessionLog — the append-only JSONL durable session log (the truth source).

One log per session at::

    {base_dir}/{session_id}/rollout.jsonl

Append-only JSONL is the canonical, crash-safe record (Codex ``rollout``).
The first line is always a ``session_meta`` event;
every subsequent line is one event.

SessionLog is the session-stream facade over the process-local EventJournal.
Callers append typed session facts and read committed envelopes; JSONL storage
shape, sequence allocation, checksums and fsync are owned by the journal.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable, Iterator, Mapping, Optional, cast

from mote.contracts.events.envelope import EventEnvelope, JsonValue, StreamId, thaw_json
from mote.contracts.ports.events.journal import AppendResult, GuardedAppendAuthority, JournalCommitGuard
from mote.runtime.events.journal import LocalEventJournal, decode_event_record
from mote.runtime.persistence import DiskWriter, disk_io
from mote.runtime.persistence.async_io import run_disk_io
from mote.runtime.session.codec import decode_session_event, encode_session_event, session_stream_id
from mote.runtime.session.events import SessionEvent, SessionMetaEvent
from mote.runtime.session.layout import SessionLayout
from mote.runtime.session.stream_ownership import SessionStreamOwnership
from mote.runtime.session.writer_guard import SessionRunWriterGuard
from mote.runtime.telemetry.logging import log_class

#: Directory name under the workspace root holding all session logs.
SESSIONS_DIRNAME = SessionLayout().sessions_dir
ROLLOUT_FILENAME = SessionLayout().rollout_file
SESSION_STREAM_MANIFEST_SCHEMA = "mote.session-stream-activation/v2"


def _default_base_dir() -> Path:
    raise ValueError("SessionLog requires an explicit base directory")


@log_class(level="DEBUG", exclude={"path", "exists"})
class SessionLog:
    """Append-only JSONL writer/reader keyed by ``session_id``."""

    def __init__(
        self,
        session_id: str,
        base_dir: Optional[str] = None,
        *,
        writer: DiskWriter | None = None,
        commit_guard: JournalCommitGuard | None = None,
        guarded_append_authority: GuardedAppendAuthority | None = None,
        stream_ownership: SessionRunWriterGuard | None = None,
    ):
        self.session_id = session_id
        base = Path(base_dir) if base_dir is not None else _default_base_dir()
        self._runtime_root = base / ".runtime"
        self._dir = base / session_id
        self._path = self._dir / ROLLOUT_FILENAME
        self._stream_id = StreamId(session_stream_id(session_id))
        self._stream_ownership = stream_ownership or SessionStreamOwnership(self._runtime_root, session_id)
        self._journal = LocalEventJournal(
            self._path,
            self._stream_id,
            writer=writer,
            commit_guard=commit_guard or self._stream_ownership,
            guarded_append_authority=(guarded_append_authority or stream_ownership),
        )
        self._schema_checked = False
        self._version = 0
        self._append_lock = threading.Lock()
        self._async_sink: Callable[[SessionEvent], Awaitable[AppendResult]] | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def writer(self) -> DiskWriter:
        return self._journal.writer

    @property
    def runtime_root(self) -> Path:
        return self._runtime_root

    @property
    def sessions_root(self) -> Path:
        return self._dir.parent

    @property
    def workspace_root(self) -> Path:
        sessions_root = self.sessions_root
        if sessions_root.name == SESSIONS_DIRNAME:
            return sessions_root.parent
        return sessions_root

    @property
    def stream_id(self) -> StreamId:
        return self._stream_id

    @property
    def event_journal(self) -> LocalEventJournal:
        return self._journal

    @property
    def committed_version(self) -> int:
        self._ensure_current_schema()
        return self._version

    @property
    def lifecycle_generation(self) -> int:
        return self._stream_ownership.lifecycle_generation

    async def start_writer(self) -> None:
        await self._stream_ownership.start()

    def release_writer(self) -> None:
        self._stream_ownership.release()

    async def close_writer(self) -> None:
        await self._stream_ownership.aclose()

    def bind_async_sink(
        self,
        sink: Callable[[SessionEvent], Awaitable[AppendResult]],
    ) -> None:
        with self._append_lock:
            if self._async_sink is not None and self._async_sink is not sink:
                raise RuntimeError("session async fact sink is already bound")
            self._async_sink = sink

    def accept_commit(self, result: AppendResult) -> None:
        if result.stream_id != self._stream_id:
            raise ValueError("session commit belongs to another stream")
        with self._append_lock:
            if self._version != result.previous_version:
                raise RuntimeError("session facade version diverged from its event fabric")
            self._version = result.current_version

    def exists(self) -> bool:
        self._ensure_current_schema()
        return self._path.exists()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------
    async def append(self, event: SessionEvent) -> AppendResult:
        """Durably append one typed fact at the facade's current stream version."""

        with self._append_lock:
            sink = self._async_sink
        if sink is not None:
            return await sink(event)
        return await run_disk_io(self.commit_offline, event)

    def commit_offline(self, event: SessionEvent) -> AppendResult:
        """Commit while no live Event Fabric owns this session stream."""

        self._ensure_current_schema()
        fact = encode_session_event(event, session_id=self.session_id)
        with self._append_lock:
            if self._async_sink is not None:
                raise RuntimeError("offline commit is forbidden after fabric binding")
            if self._version == 0 and not isinstance(event, SessionMetaEvent):
                raise ValueError("the first session fact must be SessionMetaEvent")
            if self._version > 0 and isinstance(event, SessionMetaEvent):
                raise ValueError("session metadata can only be appended once")
            if isinstance(event, SessionMetaEvent) and event.session_id != self.session_id:
                raise ValueError("session metadata identity does not match the stream")
            result = self._journal.append_committed(
                self._stream_id,
                (fact,),
                expected_version=self._version,
            )
            self._version = result.current_version
            return result

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    def iter_events(
        self,
    ) -> Iterator[EventEnvelope[Mapping[str, JsonValue]]]:
        """Yield a fully verified committed snapshot in stream order."""

        self._ensure_current_schema()
        return self._iter_identity_verified()

    def _iter_identity_verified(
        self,
    ) -> Iterator[EventEnvelope[Mapping[str, JsonValue]]]:
        saw_meta = False
        for envelope in self._journal.iter_committed(self._stream_id):
            if envelope.session_id != self.session_id:
                raise RuntimeError("Session envelope identity does not match its stream")
            event = decode_session_event(envelope)
            if isinstance(event, SessionMetaEvent):
                if saw_meta or envelope.sequence != 1:
                    raise RuntimeError("Session metadata must be the unique first fact")
                if event.session_id != self.session_id:
                    raise RuntimeError("Session metadata identity does not match its stream")
                saw_meta = True
            elif not saw_meta:
                raise RuntimeError("Session stream is missing its first metadata fact")
            yield envelope

    def _ensure_current_schema(self) -> None:
        if self._schema_checked:
            return
        self._ensure_stream_activation()
        self.writer.flush_inline()
        report = self._journal.verify_committed(self._stream_id)
        if not report.valid:
            issue = report.issues[0]
            raise RuntimeError(f"session journal integrity failure at line {issue.line}: {issue.detail}")
        self._version = report.current_version
        self._schema_checked = True

    def _ensure_stream_activation(self) -> None:
        manifest = self._dir / "stream-manifest.json"
        if manifest.exists():
            try:
                raw = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise RuntimeError("Session stream activation manifest is unreadable") from exc
            if type(raw) is not dict or raw.get("schema") != SESSION_STREAM_MANIFEST_SCHEMA:
                raise RuntimeError("Session stream activation manifest is unsupported")
            kind = raw.get("activation_kind")
            common = {
                "schema",
                "activation_kind",
                "session_id",
                "source_digest",
                "candidate_digest",
                "candidate_size",
                "projection_digest",
                "record_count",
                "evidence_retention_days",
                "legacy_production_reader",
                "activated_at",
                "retire_after",
            }
            expected = common | ({"artifact_edges_digest"} if kind == "migrated" else set())
            if (
                set(raw) != expected
                or kind not in {"empty", "migrated"}
                or raw.get("session_id") != self.session_id
                or raw.get("legacy_production_reader") != "retired"
                or type(raw.get("candidate_size")) is not int
                or type(raw.get("record_count")) is not int
            ):
                raise RuntimeError("Session stream activation manifest is not strict v2")
            candidate_size = raw["candidate_size"]
            record_count = raw["record_count"]
            try:
                activated_at = datetime.fromisoformat(raw["activated_at"])
                retire_after = datetime.fromisoformat(raw["retire_after"])
            except (TypeError, ValueError) as exc:
                raise RuntimeError("Session activation instants are invalid") from exc
            if (
                activated_at.tzinfo is None
                or retire_after.tzinfo is None
                or retire_after != activated_at + timedelta(days=180)
            ):
                raise RuntimeError("Session activation evidence retention is invalid")
            if candidate_size < 0 or record_count < 0:
                raise RuntimeError("Session stream activation bounds are invalid")
            data = self._path.read_bytes() if self._path.exists() else b""
            prefix = data[:candidate_size]
            if (
                len(prefix) != candidate_size
                or "sha256:" + hashlib.sha256(prefix).hexdigest() != raw["candidate_digest"]
            ):
                raise RuntimeError("Session stream activation candidate digest mismatch")
            projection = []
            for line in prefix.splitlines(keepends=True):
                envelope = decode_event_record(line)
                projection.append(
                    (
                        envelope.sequence,
                        str(envelope.event_type),
                        "sha256:"
                        + hashlib.sha256(
                            json.dumps(
                                thaw_json(cast(JsonValue, envelope.payload)),
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode()
                        ).hexdigest(),
                    )
                )
            projection_digest = (
                "sha256:"
                + hashlib.sha256(json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            )
            if len(projection) != record_count or projection_digest != raw["projection_digest"]:
                raise RuntimeError("Session stream activation projection digest mismatch")
            if kind == "migrated":
                edges = self._dir / "artifact-edges.v2.json"
                try:
                    edge_data = edges.read_bytes()
                except OSError as exc:
                    raise RuntimeError("Session Artifact edge candidate is unavailable") from exc
                if "sha256:" + hashlib.sha256(edge_data).hexdigest() != raw["artifact_edges_digest"]:
                    raise RuntimeError("Session Artifact edge digest mismatch")
            return
        if self._path.exists():
            raise RuntimeError("Session stream requires explicit v1 to v2 migration")
        empty_digest = "sha256:" + hashlib.sha256(b"").hexdigest()
        empty_projection = "sha256:" + hashlib.sha256(b"[]").hexdigest()
        activated_at = datetime.now(timezone.utc)
        disk_io.atomic_write(
            manifest,
            json.dumps(
                {
                    "schema": SESSION_STREAM_MANIFEST_SCHEMA,
                    "activation_kind": "empty",
                    "session_id": self.session_id,
                    "source_digest": "empty",
                    "candidate_digest": empty_digest,
                    "candidate_size": 0,
                    "projection_digest": empty_projection,
                    "record_count": 0,
                    "evidence_retention_days": 180,
                    "legacy_production_reader": "retired",
                    "activated_at": activated_at.isoformat(),
                    "retire_after": (activated_at + timedelta(days=180)).isoformat(),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
            fsync=True,
        )


__all__ = ["SessionLog", "SESSIONS_DIRNAME", "ROLLOUT_FILENAME", "SESSION_STREAM_MANIFEST_SCHEMA"]
