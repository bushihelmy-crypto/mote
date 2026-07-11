"""Session fork — branch a new session off an existing rollout (Phase 4).

A fork starts a fresh session whose history is seeded from a *parent* session at
its current state, while recording the lineage so the two stay linked. This is
the durable counterpart of Claude Code's branch/checkout and Codex's fork: the
child gets its own ``rollout.jsonl`` (independent truth source) but its
``session_meta`` carries ``parent_session_id`` pointing back at the origin.

Mechanics (disk-only; no Role needed):

  * replay the parent log to its final history (single forward pass), then
  * create the child log with a ``session_meta`` that copies the parent's
    cwd/project/model anchors and sets ``parent_session_id``, then
  * append the inherited history as ``message`` events, then
  * inherit the parent's file-history (``file_snapshot`` events + their
    before-image blobs) so the child can still diff/restore inherited files.

Replay collapses any compaction into a flat message list, so the child begins
exactly where the parent stood. The child is fully independent afterwards;
mutating it never touches the parent's log.
"""

from __future__ import annotations

from typing import Dict, Optional, Protocol
from uuid import uuid4

from mote.common.logs import log_call
from mote.session.events import FileSnapshotEvent, MessageEvent, SessionMetaEvent, parse_event
from mote.session.log import SessionLog
from mote.session.replay import replay
from mote.session.snapshot import make_blob_store


class _BlobStore(Protocol):
    """The content-addressed store slice this module uses (duck-typed).

    Both ``BlobStore`` and ``GitBlobStore`` (returned by ``make_blob_store``)
    satisfy it; annotated locally so the copy loop type-checks without pinning a
    concrete class.
    """

    def get(self, digest: str) -> Optional[bytes]:
        ...

    def put(self, content: bytes) -> str:
        ...


@log_call(level="DEBUG")
def fork(
    source_session_id: str,
    *,
    new_session_id: Optional[str] = None,
    base_dir: Optional[str] = None,
) -> str:
    """Branch a new session off ``source_session_id``; return the child's id.

    Args:
        source_session_id: The parent session to seed history from.
        new_session_id: Child session id; a fresh uuid is minted when omitted.
        base_dir: Session-log root for both logs (defaults to the workspace).

    Raises:
        FileNotFoundError: The source rollout does not exist (nothing to fork).
        FileExistsError: The child log already exists (would clobber history).
    """
    source = SessionLog(source_session_id, base_dir=base_dir)
    if not source.exists():
        raise FileNotFoundError(f"no rollout to fork for session {source_session_id!r}")

    child_id = new_session_id or uuid4().hex
    child = SessionLog(child_id, base_dir=base_dir)
    if child.exists():
        raise FileExistsError(f"fork target session {child_id!r} already exists")

    result = replay(source)  # replay scans via iter_raw, whose drain flushes the parent log first
    meta = result.meta or {}
    child.create(
        SessionMetaEvent(
            session_id=child_id,
            parent_session_id=source_session_id,
            working_dir=meta.get("working_dir", ""),
            original_working_dir=meta.get("original_working_dir", ""),
            project_root=meta.get("project_root", ""),
            model=meta.get("model"),
            role_class=meta.get("role_class"),
        )
    )
    for message in result.messages:
        child.append(MessageEvent(message=message))
    _inherit_file_history(source, child)
    return child_id


def _inherit_file_history(source: SessionLog, child: SessionLog) -> None:
    """Copy the parent's file-history (snapshot events + before-image blobs).

    ``replay`` flattens the message history but drops ``file_snapshot`` events, so
    without this the child could not diff/restore files inherited from the parent.
    Each snapshot event is re-appended in order and its referenced blob copied into
    the child's own store — the store is content-addressed, so the recorded hash is
    unchanged and the child stays a fully independent truth source. A blob missing
    in the parent is skipped (the event is still copied; diff/restore then degrade
    gracefully, exactly as they do for a parent with a missing blob).
    """
    parent_stores: Dict[str, _BlobStore] = {}
    child_stores: Dict[str, _BlobStore] = {}
    for record in source.iter_raw():
        event = parse_event(record)
        if not isinstance(event, FileSnapshotEvent):
            continue
        if event.pre_hash is not None:
            src_store = parent_stores.setdefault(event.backend, make_blob_store(source.path.parent, event.backend))
            content = src_store.get(event.pre_hash)
            if content is not None:
                dst_store = child_stores.setdefault(event.backend, make_blob_store(child.path.parent, event.backend))
                dst_store.put(content)
        child.append(event)


__all__ = ["fork"]
