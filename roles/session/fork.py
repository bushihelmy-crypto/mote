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
  * append the inherited history as ``message`` events.

Replay collapses any compaction into a flat message list, so the child begins
exactly where the parent stood. The child is fully independent afterwards;
mutating it never touches the parent's log.
"""

from __future__ import annotations

from typing import Optional
from uuid import uuid4

from metagpt.common.logs import log_call
from metagpt.roles.session.events import MessageEvent, SessionMetaEvent
from metagpt.roles.session.log import SessionLog
from metagpt.roles.session.replay import replay


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

    result = replay(source)
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
    return child_id


__all__ = ["fork"]
