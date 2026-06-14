"""Session persistence — the append-only durable session log (Phase 1).

Records an agent session's lifecycle to a crash-safe JSONL rollout (Codex
``rollout`` + Claude Code transcript synthesis). The truth source for future
resume/list. Core pieces:

* :mod:`events` — the tagged-union event schema + line (de)serialization.
* :class:`SessionLog` — append-only JSONL writer/reader keyed by session_id.
* :class:`SessionRecorder` — the sink injected into ``ContextManager`` (conforms
  to ``metagpt.common.interface.SessionRecorder``).
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
from metagpt.session.recorder import SessionRecorder
from metagpt.session.replay import ReplayResult, replay
from metagpt.session.snapshot import BlobStore, FileSnapshotRecorder

__all__ = [
    "SessionLog",
    "SessionRecorder",
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
