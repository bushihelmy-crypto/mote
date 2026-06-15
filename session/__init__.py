"""Session persistence — the append-only durable session log (Phase 1).

Records an agent session's lifecycle to a crash-safe JSONL rollout (Codex
``rollout`` + Claude Code transcript synthesis). The truth source for future
resume/list. Core pieces:

* :mod:`events` — the tagged-union event schema + line (de)serialization.
* :class:`SessionLog` — append-only JSONL writer/reader keyed by session_id.
* :class:`RecorderSubscriber` — the event-bus subscriber that streams the
  agent's lifecycle events to a :class:`SessionLog` (replaces the old
  ``SessionRecorder`` sink injected into ``ContextManager``).
"""

from metagpt.session.events import (
    CompactedEvent,
    FileSnapshotEvent,
    MessageEvent,
    MetaUpdateEvent,
    SessionMetaEvent,
    TurnContextEvent,
)
from metagpt.session.fork import fork
from metagpt.session.history import (
    SnapshotEntry,
    diff_snapshot,
    file_history,
    restore,
)
from metagpt.session.listing import SessionInfo, list_sessions
from metagpt.session.log import SessionLog
from metagpt.session.replay import ReplayResult, replay
from metagpt.session.snapshot import BlobStore, FileSnapshotRecorder
from metagpt.session.subscribers import RecorderSubscriber

__all__ = [
    "SessionLog",
    "RecorderSubscriber",
    "SessionMetaEvent",
    "MessageEvent",
    "CompactedEvent",
    "TurnContextEvent",
    "MetaUpdateEvent",
    "FileSnapshotEvent",
    "ReplayResult",
    "replay",
    "SessionInfo",
    "list_sessions",
    "fork",
    "BlobStore",
    "FileSnapshotRecorder",
    "SnapshotEntry",
    "file_history",
    "diff_snapshot",
    "restore",
]
