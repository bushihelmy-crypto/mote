"""File-history queries — diff / restore over the session's before-images (Phase 2).

Phase 1 captures a :class:`~metagpt.session.events.FileSnapshotEvent` (plus
a content-addressed blob) just before every file-mutating tool overwrites a file.
This module turns that append-only record into the read side: list a file's
history, diff a stored before-image against what is on disk now, and restore a
file back to one of its captured states.

Everything is a forward scan of the same ``rollout.jsonl`` the session already
owns — no second index. The blob bytes come from the session's
:class:`~metagpt.session.snapshot.BlobStore`, rooted next to the log.
"""

from __future__ import annotations

import difflib
import os
from dataclasses import dataclass
from typing import Dict, List, Optional

from metagpt.session.events import FILE_SNAPSHOT
from metagpt.session.log import SessionLog
from metagpt.session.snapshot import make_blob_store


@dataclass
class SnapshotEntry:
    """One captured before-image of a file (a flattened ``FileSnapshotEvent``).

    ``index`` is the position of this snapshot within the file's own history
    (0-based, chronological); ``ts`` is the event's ISO timestamp.
    """

    path: str
    operation: str  # create | update
    pre_hash: Optional[str]
    pre_size: int
    display_path: str
    tool: str
    ts: str
    index: int
    backend: str = "blob"  # which store holds the blob: "blob" | "git"

    @property
    def existed(self) -> bool:
        """Whether the file existed before this mutation (``update`` vs ``create``)."""
        return self.operation != "create"


def _blobs_for(log: SessionLog, backend: str = "blob"):
    """The content-addressed store for ``backend``, rooted next to the log."""
    return make_blob_store(log.path.parent, backend)


def file_history(log: SessionLog) -> Dict[str, List[SnapshotEntry]]:
    """Group every ``file_snapshot`` event by path, in chronological order.

    Returns ``{path -> [SnapshotEntry, ...]}`` where each list is ordered oldest
    → newest and each entry's ``index`` is its position in that list.
    """
    history: Dict[str, List[SnapshotEntry]] = {}
    for record in log.iter_raw():
        if record.get("type") != FILE_SNAPSHOT:
            continue
        payload = record.get("payload") or {}
        path = payload.get("path")
        if not path:
            continue
        bucket = history.setdefault(path, [])
        bucket.append(
            SnapshotEntry(
                path=path,
                operation=payload.get("operation", "update"),
                pre_hash=payload.get("pre_hash"),
                pre_size=payload.get("pre_size", 0),
                display_path=payload.get("display_path") or path,
                tool=payload.get("tool", ""),
                ts=record.get("ts", ""),
                index=len(bucket),
                backend=payload.get("backend", "blob"),
            )
        )
    return history


def _entry_at(log: SessionLog, path: str, index: int) -> Optional[SnapshotEntry]:
    entries = file_history(log).get(path)
    if not entries:
        return None
    try:
        return entries[index]
    except IndexError:
        return None


def diff_snapshot(log: SessionLog, path: str, *, index: int = -1) -> str:
    """Unified diff of a captured before-image against the current on-disk file.

    ``index`` selects which snapshot of ``path`` to compare (default ``-1`` =
    the most recent before-image). Returns the unified diff text, or ``""`` when
    there is no difference. Raises ``KeyError`` if the path has no history and
    ``IndexError`` if ``index`` is out of range.
    """
    entry = _entry_at(log, path, index)
    if entry is None:
        raise KeyError(f"no file-history for '{path}'")

    if entry.pre_hash is None:
        before = b""  # file did not exist before this mutation (a create)
    else:
        before = _blobs_for(log, entry.backend).get(entry.pre_hash) or b""

    try:
        with open(path, "rb") as f:
            current = f.read()
    except OSError:
        current = b""

    before_text = before.decode("utf-8", errors="replace").splitlines(keepends=True)
    current_text = current.decode("utf-8", errors="replace").splitlines(keepends=True)

    diff = difflib.unified_diff(
        before_text,
        current_text,
        fromfile=f"{entry.display_path} (before {entry.tool or 'mutation'})",
        tofile=f"{entry.display_path} (current)",
    )
    return "".join(diff)


def restore(log: SessionLog, path: str, *, index: int = -1) -> bool:
    """Restore ``path`` on disk to one of its captured before-images.

    ``index`` selects which snapshot to restore (default ``-1`` = the most
    recent before-image). A ``create`` before-image (the file did not yet
    exist) is restored by removing the file. Returns ``True`` on success,
    ``False`` if the referenced blob is missing. Raises ``KeyError`` if the
    path has no history and ``IndexError`` if ``index`` is out of range.
    """
    entry = _entry_at(log, path, index)
    if entry is None:
        raise KeyError(f"no file-history for '{path}'")

    if entry.pre_hash is None:
        # Before-image was "absent": restoring means the file should not exist.
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return True

    content = _blobs_for(log, entry.backend).get(entry.pre_hash)
    if content is None:
        return False

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.restore.tmp.{os.getpid()}"
    with open(tmp, "wb") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return True


__all__ = ["SnapshotEntry", "file_history", "diff_snapshot", "restore"]
