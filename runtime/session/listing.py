"""Session listing — discover resumable sessions cheaply.

A "lite" strategy: never full-parse a rollout just to list it. For
each ``{base}/{session_id}/rollout.jsonl`` we read only two small windows:

  * a HEAD window for the ``session_meta`` first line, the first ``message``
    (human-readable preview), and the ``meta_update`` title/last_prompt —
    ``TitleSubscriber`` appends its single title during the first turn
    (fire-and-forget on the first prompt), so it lives near the HEAD, and
  * a TAIL window (last bytes) as a last-write-wins override, so any future
    end-of-session ``meta_update`` (e.g. a rename) still wins over the head.

Results are sorted by file mtime (newest first), optionally filtered by cwd.
This keeps listing O(open + small reads) per session regardless of transcript
size, mirroring Claude's head/tail scan; a SQLite index is a later option for
scale and is intentionally not built here (the rollout stays the truth source).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from mote.runtime.events.journal import decode_event_record
from mote.runtime.logging import log_call
from mote.runtime.session.codec import decode_session_event
from mote.runtime.session.events import MessageEvent, MetaUpdateEvent, SessionMetaEvent
from mote.runtime.session.log import ROLLOUT_FILENAME, _default_base_dir

#: How many leading bytes to scan for meta + first message + the head title.
#: The title is written fire-and-forget during the first turn, so it lands near
#: the head but past a tight line window on a busy turn — a byte window (mirror
#: of the tail) catches it regardless of how many events the first turn emits.
_HEAD_BYTES = 65536
#: How many trailing bytes to scan for the latest meta_update (tail window).
_TAIL_BYTES = 65536
#: Max preview length kept from the first message.
_PREVIEW_MAX = 200


@dataclass
class SessionInfo:
    """Lite metadata about one resumable session (no full history load)."""

    session_id: str
    path: str
    modified_ts: float
    size: int = 0
    created_at: Optional[str] = None
    working_dir: Optional[str] = None
    project_root: Optional[str] = None
    model: Optional[str] = None
    role_class: Optional[str] = None
    parent_session_id: Optional[str] = None
    title: Optional[str] = None
    last_prompt: Optional[str] = None
    preview: Optional[str] = None

    @property
    def modified(self) -> str:
        return datetime.fromtimestamp(self.modified_ts).isoformat()


def _read_head(
    path: Path,
) -> tuple[Optional[SessionMetaEvent], Optional[str], Optional[str], Optional[str]]:
    """Return (session_meta, first-message preview, head title, head last_prompt).

    The title/last_prompt walk newest-first *within* the head window so a rename
    early in the session already wins there (last-write-wins); the tail scan then
    overrides with any later end-of-session meta_update.
    """
    with open(path, "rb") as f:
        chunk = f.read(_HEAD_BYTES)
    lines = chunk.decode("utf-8", errors="ignore").split("\n")
    meta: Optional[SessionMetaEvent] = None
    preview: Optional[str] = None
    title: Optional[str] = None
    last_prompt: Optional[str] = None
    for line in lines:
        try:
            event = decode_session_event(decode_event_record(line))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(event, SessionMetaEvent) and meta is None:
            meta = event
        elif isinstance(event, MessageEvent) and preview is None and event.message is not None:
            content = event.message.content
            if isinstance(content, str):
                preview = content[:_PREVIEW_MAX]
        elif isinstance(event, MetaUpdateEvent):
            if event.title:
                title = event.title  # last-write-wins within the head window
            if event.last_prompt:
                last_prompt = event.last_prompt
    return meta, preview, title, last_prompt


def _read_tail_meta(path: Path, size: int) -> tuple[Optional[str], Optional[str]]:
    """Scan the trailing window for the latest meta_update (title, last_prompt)."""
    if size == 0:
        return None, None
    start = max(0, size - _TAIL_BYTES)
    with open(path, "rb") as f:
        f.seek(start)
        chunk = f.read()
    text = chunk.decode("utf-8", errors="ignore")
    lines = text.split("\n")
    if start > 0 and lines:
        lines = lines[1:]  # drop the partial first line
    title: Optional[str] = None
    last_prompt: Optional[str] = None
    # Walk newest-first so the first hit wins (last-write-wins semantics).
    for line in reversed(lines):
        try:
            event = decode_session_event(decode_event_record(line))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(event, MetaUpdateEvent):
            continue
        if title is None and event.title:
            title = event.title
        if last_prompt is None and event.last_prompt:
            last_prompt = event.last_prompt
        if title is not None and last_prompt is not None:
            break
    return title, last_prompt


def _read_lite(rollout: Path, session_id: str) -> Optional[SessionInfo]:
    try:
        stat = rollout.stat()
    except OSError:
        return None
    try:
        meta, preview, head_title, head_prompt = _read_head(rollout)
        tail_title, tail_prompt = _read_tail_meta(rollout, stat.st_size)
    except OSError:
        return None
    # Tail is the newest window: a later end-of-session meta_update (e.g. a
    # rename) overrides the head title; else fall back to what the head caught
    # (where TitleSubscriber writes its one-shot title during the first turn).
    title = tail_title if tail_title is not None else head_title
    last_prompt = tail_prompt if tail_prompt is not None else head_prompt
    return SessionInfo(
        session_id=(meta.session_id if meta else None) or session_id,
        path=str(rollout),
        modified_ts=stat.st_mtime,
        size=stat.st_size,
        created_at=meta.created_at if meta else None,
        working_dir=meta.working_dir if meta else None,
        project_root=meta.project_root if meta else None,
        model=meta.model if meta else None,
        role_class=meta.role_class if meta else None,
        parent_session_id=meta.parent_session_id if meta else None,
        title=title,
        last_prompt=last_prompt,
        preview=preview,
    )


@log_call(level="DEBUG")
def list_sessions(base_dir: Optional[str] = None, *, cwd: Optional[str] = None) -> List[SessionInfo]:
    """List resumable sessions, newest first.

    Args:
        base_dir: Session-log root; defaults to ``{workspace}/.agent_sessions``.
        cwd: When set, keep only sessions whose ``working_dir`` or
            ``project_root`` equals it (Claude same-repo filter).
    """
    base = Path(base_dir) if base_dir is not None else _default_base_dir()
    if not base.exists():
        return []

    infos: List[SessionInfo] = []
    for child in base.iterdir():
        if not child.is_dir():
            continue
        rollout = child / ROLLOUT_FILENAME
        if not rollout.exists():
            continue
        info = _read_lite(rollout, child.name)
        if info is None:
            continue
        if cwd is not None and info.working_dir != cwd and info.project_root != cwd:
            continue
        infos.append(info)

    infos.sort(key=lambda i: i.modified_ts, reverse=True)
    return infos


__all__ = ["SessionInfo", "list_sessions"]
