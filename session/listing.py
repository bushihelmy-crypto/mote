"""Session listing — discover resumable sessions cheaply (Phase 3).

Claude Code "lite" strategy: never full-parse a rollout just to list it. For
each ``{base}/{session_id}/rollout.jsonl`` we read only two small windows:

  * a HEAD window (first few lines) for the ``session_meta`` first line and the
    first ``message`` as a human-readable preview, and
  * a TAIL window (last bytes) for the latest ``meta_update`` (title /
    last_prompt), which the writer keeps near EOF for exactly this reason.

Results are sorted by file mtime (newest first), optionally filtered by cwd.
This keeps listing O(open + small reads) per session regardless of transcript
size, mirroring Claude's head/tail scan; a SQLite index is a later option for
scale and is intentionally not built here (the rollout stays the truth source).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from metagpt.common.logs import log_call
from metagpt.session.events import (
    MessageEvent,
    MetaUpdateEvent,
    SessionMetaEvent,
    parse_event,
    parse_line,
)
from metagpt.session.log import ROLLOUT_FILENAME, _default_base_dir

#: How many leading lines to scan for the meta + first message preview.
_HEAD_LINES = 16
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


def _read_head(path: Path) -> tuple[Optional[SessionMetaEvent], Optional[str]]:
    """Return (session_meta event, first-message preview) from the head."""
    meta: Optional[SessionMetaEvent] = None
    preview: Optional[str] = None
    with open(path, "r", encoding="utf-8") as f:
        for _ in range(_HEAD_LINES):
            line = f.readline()
            if not line:
                break
            event = parse_event(parse_line(line) or {})
            if isinstance(event, SessionMetaEvent) and meta is None:
                meta = event
            elif isinstance(event, MessageEvent) and preview is None and event.message is not None:
                content = event.message.content
                if isinstance(content, str):
                    preview = content[:_PREVIEW_MAX]
            if meta is not None and preview is not None:
                break
    return meta, preview


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
        event = parse_event(parse_line(line) or {})
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
        meta, preview = _read_head(rollout)
        title, last_prompt = _read_tail_meta(rollout, stat.st_size)
    except OSError:
        return None
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
