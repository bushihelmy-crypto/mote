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

import threading
from pathlib import Path
from typing import Awaitable, Callable, Iterator, Mapping, Optional

from mote.contracts.events.envelope import EventEnvelope, JsonValue, StreamId
from mote.contracts.ports.events.journal import AppendResult
from mote.runtime.events.journal import LocalEventJournal
from mote.runtime.persistence import DiskWriter
from mote.runtime.persistence.async_io import run_disk_io
from mote.runtime.session.codec import decode_session_event, encode_session_event, session_stream_id
from mote.runtime.session.events import SessionEvent, SessionMetaEvent
from mote.runtime.session.layout import SessionLayout
from mote.runtime.telemetry.logging import log_class

#: Directory name under the workspace root holding all session logs.
SESSIONS_DIRNAME = SessionLayout().sessions_dir
ROLLOUT_FILENAME = SessionLayout().rollout_file


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
    ):
        self.session_id = session_id
        base = Path(base_dir) if base_dir is not None else _default_base_dir()
        self._runtime_root = base / ".runtime"
        self._dir = base / session_id
        self._path = self._dir / ROLLOUT_FILENAME
        self._stream_id = StreamId(session_stream_id(session_id))
        self._journal = LocalEventJournal(
            self._path,
            self._stream_id,
            writer=writer,
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
        self.writer.flush_inline()
        report = self._journal.verify_committed(self._stream_id)
        if not report.valid:
            issue = report.issues[0]
            raise RuntimeError(f"session journal integrity failure at line {issue.line}: {issue.detail}")
        self._version = report.current_version
        self._schema_checked = True


__all__ = ["SessionLog", "SESSIONS_DIRNAME", "ROLLOUT_FILENAME"]
