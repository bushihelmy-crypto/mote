"""SessionLog — the append-only JSONL durable session log (the truth source).

One log per session at::

    {base_dir}/{session_id}/rollout.jsonl

Append-only JSONL is the canonical, crash-safe record (Codex ``rollout`` +
Claude Code transcript). The first line is always a ``session_meta`` event;
every subsequent line is one event. Writes use ``O_APPEND`` + ``flush`` so a
crash never corrupts earlier lines and at most loses the in-flight write.

Phase 1 writes synchronously (small, infrequent JSON lines): correctness over
throughput. Async/batched writing is a later optimization.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator, Optional

from metagpt.common.logs import log_class
from metagpt.roles.session.events import SessionEvent, SessionMetaEvent, parse_line, to_line

#: Directory name under the workspace root holding all session logs.
SESSIONS_DIRNAME = ".agent_sessions"
#: The rollout file name inside each session directory.
ROLLOUT_FILENAME = "rollout.jsonl"


def _default_base_dir() -> Path:
    from metagpt.common.const import DEFAULT_WORKSPACE_ROOT

    return Path(DEFAULT_WORKSPACE_ROOT) / SESSIONS_DIRNAME


@log_class(level="DEBUG", exclude={"path", "exists"})
class SessionLog:
    """Append-only JSONL writer/reader keyed by ``session_id``."""

    def __init__(self, session_id: str, base_dir: Optional[str] = None):
        self.session_id = session_id
        base = Path(base_dir) if base_dir is not None else _default_base_dir()
        self._dir = base / session_id
        self._path = self._dir / ROLLOUT_FILENAME

    @property
    def path(self) -> Path:
        return self._path

    def exists(self) -> bool:
        return self._path.exists()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------
    def create(self, meta: SessionMetaEvent) -> bool:
        """Write the ``session_meta`` first line for a fresh log.

        No-ops (returns False) when the log already exists, so resume/restart
        never double-writes metadata or truncates history.
        """
        if self._path.exists():
            return False
        self._dir.mkdir(parents=True, exist_ok=True)
        self._write_line(to_line(meta))
        return True

    def append(self, event: SessionEvent) -> None:
        """Append one event as a JSONL line (O_APPEND + flush)."""
        self._dir.mkdir(parents=True, exist_ok=True)
        self._write_line(to_line(event))

    def _write_line(self, line: str) -> None:
        # Open in append mode per write: O_APPEND makes concurrent appends
        # atomic at the line level and keeps earlier lines intact on crash.
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()

    # ------------------------------------------------------------------
    # Read (raw; typed reconstruction is Phase 2)
    # ------------------------------------------------------------------
    def iter_raw(self) -> Iterator[dict]:
        """Yield each parsed ``{type, ts, payload}`` record, skipping bad lines."""
        if not self._path.exists():
            return
        with open(self._path, "r", encoding="utf-8") as f:
            for line in f:
                record = parse_line(line)
                if record is not None:
                    yield record


__all__ = ["SessionLog", "SESSIONS_DIRNAME", "ROLLOUT_FILENAME"]
