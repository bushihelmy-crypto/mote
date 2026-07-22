"""Session persistence — the append-only durable session log (Phase 1).

Records an agent session's lifecycle to a crash-safe JSONL rollout (Codex
``rollout`` synthesis). The truth source for future
resume/list. Core pieces:

* :mod:`events` — the tagged-union event schema + line (de)serialization.
* :class:`SessionLog` — append-only JSONL writer/reader keyed by session_id.
* :class:`RecorderSubscriber` — the event-bus subscriber that streams the
  agent's lifecycle events to a :class:`SessionLog`.
"""

from mote.session.attribution import HunkAttribution, HunkView, SessionSummary
from mote.session.browser_state import BrowserStateRecorder
from mote.session.checkpoint import CheckpointEntry, CheckpointStore, list_checkpoints
from mote.session.events import (
    BrowserStateEvent,
    CheckpointEvent,
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
from mote.session.hunk_ledger import HunkLedger, HunkRecord
from mote.session.ids import new_session_id
from mote.session.kernel_state import KernelStateRecorder
from mote.session.listing import SessionInfo, list_sessions
from mote.session.log import SessionLog
from mote.session.recall import body_for_tool_call
from mote.session.reconcile import ReconcileResult, reconcile_tool_calls
from mote.session.replay import ReplayResult, replay
from mote.session.snapshot import BlobStore, FileSnapshotRecorder
from mote.session.subscribers import CheckpointSubscriber, HunkSubscriber, RecorderSubscriber, TitleSubscriber
from mote.session.terminal_state import TerminalStateRecorder

__all__ = [
    "SessionLog",
    "RecorderSubscriber",
    "TitleSubscriber",
    "CheckpointSubscriber",
    "HunkSubscriber",
    "SessionMetaEvent",
    "MessageEvent",
    "CompactedEvent",
    "TurnContextEvent",
    "MetaUpdateEvent",
    "FileSnapshotEvent",
    "CheckpointEvent",
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
    "CheckpointStore",
    "CheckpointEntry",
    "list_checkpoints",
    "body_for_tool_call",
    "HunkLedger",
    "HunkRecord",
    "HunkAttribution",
    "HunkView",
    "SessionSummary",
]
