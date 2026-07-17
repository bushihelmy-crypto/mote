"""SessionLog — the append-only JSONL durable session log (the truth source).

One log per session at::

    {base_dir}/{session_id}/rollout.jsonl

Append-only JSONL is the canonical, crash-safe record (Codex ``rollout``).
The first line is always a ``session_meta`` event;
every subsequent line is one event.

SessionLog now **composes a** :class:`~mote.common.disk.Journal` for the
line-level append/scan mechanics: it keeps the event-shaped API
(``create``/``append``/``iter_raw``) and only adds the event<->line
(de)serialization (``to_line``/``parse_line``). The journal routes writes
through the shared :class:`~mote.common.disk.DiskWriter` (per-path FIFO so the
``session_meta`` first line always lands first), and a ``drain()`` barrier flushes
them at turn boundaries / shutdown / before any in-process replay.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, Optional

from mote.common.const import DEFAULT_WORKSPACE_ROOT, ROLLOUT_FILENAME, SESSIONS_SUBDIR
from mote.common.disk import Journal, drain_blocking
from mote.common.logs import log_class
from mote.session.events import SessionEvent, SessionMetaEvent, parse_line, to_line

#: Directory name under the workspace root holding all session logs. Back-compat
#: alias for the centralized :data:`mote.common.const.SESSIONS_SUBDIR`.
SESSIONS_DIRNAME = SESSIONS_SUBDIR


def _default_base_dir() -> Path:
    return Path(DEFAULT_WORKSPACE_ROOT) / SESSIONS_DIRNAME


@log_class(level="DEBUG", exclude={"path", "exists"})
class SessionLog:
    """Append-only JSONL writer/reader keyed by ``session_id``."""

    def __init__(self, session_id: str, base_dir: Optional[str] = None):
        self.session_id = session_id
        base = Path(base_dir) if base_dir is not None else _default_base_dir()
        self._dir = base / session_id
        self._path = self._dir / ROLLOUT_FILENAME
        self._journal = Journal(self._path)

    @property
    def path(self) -> Path:
        return self._path

    def exists(self) -> bool:
        return self._journal.exists()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------
    def create(self, meta: SessionMetaEvent) -> bool:
        """Write the ``session_meta`` first line for a fresh log.

        No-ops (returns False) when the log already exists, so resume/restart
        never double-writes metadata or truncates history.
        """
        return self._journal.create_if_absent(to_line(meta))

    def append(self, event: SessionEvent) -> None:
        """Append one event as a JSONL line (ordered via the DiskWriter)."""
        self._journal.append_line(to_line(event))

    # ------------------------------------------------------------------
    # Read (raw; typed reconstruction is Phase 2)
    # ------------------------------------------------------------------
    def iter_raw(self) -> Iterator[dict]:
        """Yield each parsed ``{type, ts, payload}`` record, skipping bad lines.

        Flushes the shared :class:`~mote.common.disk.DiskWriter` first so any
        queued appends (and queued blob writes) are on disk before the scan —
        the single durability barrier every in-process read path (replay / fork /
        file-history) relies on, so callers no longer drain by hand.
        """
        drain_blocking()
        for line in self._journal.iter_raw_lines():
            record = parse_line(line)
            if record is not None:
                yield record


__all__ = ["SessionLog", "SESSIONS_DIRNAME", "ROLLOUT_FILENAME"]
