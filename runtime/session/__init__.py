"""Session persistence — the append-only durable session log.

Records an agent session's lifecycle to a crash-safe JSONL rollout (Codex
``rollout`` synthesis). The truth source for future
resume/list. Core pieces:

* :mod:`events` — the tagged-union event schema + line (de)serialization.
* :class:`SessionLog` — append-only JSONL writer/reader keyed by session_id.
* :class:`SessionFactCommitter` — explicit durable commit boundary.
"""

from mote.runtime.session.attribution import HunkAttribution, HunkView, SessionSummary
from mote.runtime.session.checkpoint import CheckpointEntry, list_checkpoints
from mote.runtime.session.committer import SessionFactCommitter
from mote.runtime.session.events import (
    CheckpointEvent,
    ContextCompactedFact,
    FileHistoryImportedEvent,
    HistoryEditedFact,
    MessageEvent,
    MetaUpdateEvent,
    RuntimeCheckpointEvent,
    RuntimeCommitEvent,
    RuntimeHandoffActivatedEvent,
    RuntimeHandoffPreparedEvent,
    RuntimeHandoffResolvedEvent,
    RuntimeProjectionAcknowledgedEvent,
    SessionMetaEvent,
    TurnContextEvent,
)
from mote.runtime.session.history import SnapshotEntry, diff_snapshot, file_history, restore
from mote.runtime.session.ids import new_session_id
from mote.runtime.session.listing import SessionInfo, list_sessions
from mote.runtime.session.log import SessionLog
from mote.runtime.session.recall import body_for_tool_call
from mote.runtime.session.reconcile import ReconcileResult, reconcile_tool_calls
from mote.runtime.session.run_lease import RunLeaseHandle, RunLeaseStore
from mote.runtime.session.runtime_checkpoint import RuntimeCheckpointRecorder
from mote.runtime.session.runtime_projection import SessionRuntimeProjectionJournal
from mote.runtime.session.subscribers import CheckpointSubscriber, TitleSubscriber

__all__ = [
    "SessionLog",
    "SessionFactCommitter",
    "TitleSubscriber",
    "CheckpointSubscriber",
    "SessionMetaEvent",
    "MessageEvent",
    "ContextCompactedFact",
    "HistoryEditedFact",
    "TurnContextEvent",
    "MetaUpdateEvent",
    "FileHistoryImportedEvent",
    "CheckpointEvent",
    "RuntimeCheckpointEvent",
    "RuntimeCommitEvent",
    "RuntimeHandoffPreparedEvent",
    "RuntimeHandoffActivatedEvent",
    "RuntimeHandoffResolvedEvent",
    "RuntimeProjectionAcknowledgedEvent",
    "RuntimeCheckpointRecorder",
    "SessionRuntimeProjectionJournal",
    "ReconcileResult",
    "reconcile_tool_calls",
    "SessionInfo",
    "list_sessions",
    "new_session_id",
    "SnapshotEntry",
    "file_history",
    "diff_snapshot",
    "restore",
    "CheckpointEntry",
    "list_checkpoints",
    "body_for_tool_call",
    "HunkAttribution",
    "HunkView",
    "SessionSummary",
    "RunLeaseHandle",
    "RunLeaseStore",
]
