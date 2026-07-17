"""Session persistence — the append-only durable session log (Phase 1).

Records an agent session's lifecycle to a crash-safe JSONL rollout (Codex
``rollout`` synthesis). The truth source for future
resume/list. Core pieces:

* :mod:`events` — the tagged-union event schema + line (de)serialization.
* :class:`SessionLog` — append-only JSONL writer/reader keyed by session_id.
* :class:`RecorderSubscriber` — the event-bus subscriber that streams the
  agent's lifecycle events to a :class:`SessionLog`.
"""

from mote.session.browser_state import BrowserStateRecorder
from mote.session.events import (
    BrowserStateEvent,
    CompactedEvent,
    FileSnapshotEvent,
    KernelStateEvent,
    MessageEvent,
    MetaUpdateEvent,
    SessionMetaEvent,
    TerminalStateEvent,
    TurnContextEvent,
)
from mote.session.fork import fork
from mote.session.history import SnapshotEntry, diff_snapshot, file_history, restore
from mote.session.ids import new_session_id
from mote.session.kernel_state import KernelStateRecorder
from mote.session.listing import SessionInfo, list_sessions
from mote.session.log import SessionLog
from mote.session.recall import body_for_tool_call
from mote.session.reconcile import ReconcileResult, reconcile_tool_calls
from mote.session.replay import ReplayResult, replay
from mote.session.snapshot import BlobStore, FileSnapshotRecorder
from mote.session.subscribers import RecorderSubscriber
from mote.session.terminal_state import TerminalStateRecorder

__all__ = [
    "SessionLog",
    "RecorderSubscriber",
    "SessionMetaEvent",
    "MessageEvent",
    "CompactedEvent",
    "TurnContextEvent",
    "MetaUpdateEvent",
    "FileSnapshotEvent",
    "TerminalStateEvent",
    "TerminalStateRecorder",
    "KernelStateEvent",
    "KernelStateRecorder",
    "BrowserStateEvent",
    "BrowserStateRecorder",
    "ReplayResult",
    "replay",
    "ReconcileResult",
    "reconcile_tool_calls",
    "SessionInfo",
    "list_sessions",
    "fork",
    "new_session_id",
    "BlobStore",
    "FileSnapshotRecorder",
    "SnapshotEntry",
    "file_history",
    "diff_snapshot",
    "restore",
    "body_for_tool_call",
]
