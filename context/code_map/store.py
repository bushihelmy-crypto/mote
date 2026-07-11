"""CodeMapStore — persist :class:`FileExtract` rows into a small SQLite graph.

The store is the *persistence* half of the local code map. It holds two tables:

- ``nodes`` — one row per :class:`Symbol` (function / method / class), keyed by
  the file it lives in, so the map can answer "what does this file define?";
- ``edges`` — one row per structural edge, of two kinds:
  - ``imports`` — ``source_file`` imports module-name ``target`` (file->module);
  - ``calls`` — inside ``source_file``, qualified ``caller`` invokes bare
    ``target`` callee (a same-file symbol->symbol edge).

Deliberately *not* CodeGraph's schema: no FTS5, no visibility / decorators /
docstrings / language columns, no cross-file id resolution. This map is
Python-only and *locality-scoped* — it only ever holds files the agent touched,
so a whole-repo search index (FTS5) would be dead weight. Reverse-dependency
queries are answered by string-matching import targets against candidate module
names computed in the facade, not by a resolved node-id graph.

Idempotent by file: :meth:`upsert_file` deletes every prior row for a path and
re-inserts, so re-parsing a changed file never accumulates stale symbols/edges.
The connection is opened ``check_same_thread=False`` — all use is single-thread
today, but the flag keeps a stray event-loop-thread access from raising.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Iterable

from mote.context.code_map.extractor import CallEdge, FileExtract, Symbol

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    file_path TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    signature TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_nodes_file ON nodes(file_path);

CREATE TABLE IF NOT EXISTS edges (
    source_file TEXT NOT NULL,
    target TEXT NOT NULL,
    kind TEXT NOT NULL,
    caller TEXT NOT NULL DEFAULT '',
    line INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_file, kind);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target, kind);

CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    indexed_at INTEGER
);
"""


class CodeMapStore:
    """A tiny SQLite nodes/edges store for the local code map.

    Pass ``":memory:"`` (the default) for an ephemeral per-session map, or a file
    path to persist across sessions. The schema is created on construction.
    """

    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- writes --------------------------------------------------------------

    def upsert_file(self, extract: FileExtract) -> None:
        """Replace all rows for ``extract.path`` with its current symbols/edges.

        Idempotent: a prior parse of the same file is fully cleared first, so a
        re-parse after an edit never leaves stale symbols or edges behind.
        """
        path = extract.path
        cur = self._conn.cursor()
        cur.execute("DELETE FROM nodes WHERE file_path = ?", (path,))
        cur.execute("DELETE FROM edges WHERE source_file = ?", (path,))

        cur.executemany(
            "INSERT INTO nodes (file_path, name, qualified_name, kind, start_line, signature) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(path, s.name, s.qualified_name, s.kind, s.start_line, s.signature) for s in extract.symbols],
        )
        cur.executemany(
            "INSERT INTO edges (source_file, target, kind, caller, line) VALUES (?, ?, 'imports', '', 0)",
            [(path, module) for module in extract.imports],
        )
        cur.executemany(
            "INSERT INTO edges (source_file, target, kind, caller, line) VALUES (?, ?, 'calls', ?, ?)",
            [(path, c.callee, c.caller, c.line) for c in extract.calls],
        )
        # The files row is the staleness anchor: content hash + parse time. An
        # INSERT-OR-REPLACE keyed on the path keeps exactly one row per file.
        cur.execute(
            "INSERT OR REPLACE INTO files (path, content_hash, indexed_at) VALUES (?, ?, ?)",
            (path, extract.content_hash, int(time.time())),
        )
        self._conn.commit()

    def delete_file(self, path: str) -> None:
        """Drop all rows for ``path`` (a vanished file). Idempotent."""
        cur = self._conn.cursor()
        cur.execute("DELETE FROM nodes WHERE file_path = ?", (path,))
        cur.execute("DELETE FROM edges WHERE source_file = ?", (path,))
        cur.execute("DELETE FROM files WHERE path = ?", (path,))
        self._conn.commit()

    # -- reads ---------------------------------------------------------------

    def symbols_in(self, file_path: str) -> list[Symbol]:
        """Every symbol defined in ``file_path``, ordered by definition line."""
        rows = self._conn.execute(
            "SELECT name, qualified_name, kind, start_line, signature FROM nodes "
            "WHERE file_path = ? ORDER BY start_line",
            (file_path,),
        ).fetchall()
        return [Symbol(name=r[0], qualified_name=r[1], kind=r[2], start_line=r[3], signature=r[4]) for r in rows]

    def calls_in(self, file_path: str) -> list[CallEdge]:
        """Every intra-file call edge recorded for ``file_path``, in call order."""
        rows = self._conn.execute(
            "SELECT caller, target, line FROM edges WHERE source_file = ? AND kind = 'calls' ORDER BY line",
            (file_path,),
        ).fetchall()
        return [CallEdge(caller=r[0], callee=r[1], line=r[2]) for r in rows]

    def imports_of(self, file_path: str) -> list[str]:
        """Module names ``file_path`` imports (order preserved, as inserted)."""
        rows = self._conn.execute(
            "SELECT target FROM edges WHERE source_file = ? AND kind = 'imports' ORDER BY rowid",
            (file_path,),
        ).fetchall()
        return [r[0] for r in rows]

    def importers_within(self, targets: Iterable[str], scope_files: Iterable[str]) -> list[str]:
        """Files in ``scope_files`` that import any name in ``targets``.

        The reverse-dependency query. ``targets`` are the module names by which a
        file could be imported (the facade computes these from the file's dotted
        path); ``scope_files`` is the touched set, so we never report importers
        outside the locality the agent is working in.
        """
        target_list = list(targets)
        scope_list = list(scope_files)
        if not target_list or not scope_list:
            return []
        tq = ",".join("?" for _ in target_list)
        sq = ",".join("?" for _ in scope_list)
        rows = self._conn.execute(
            f"SELECT DISTINCT source_file FROM edges "
            f"WHERE kind = 'imports' AND target IN ({tq}) AND source_file IN ({sq})",
            (*target_list, *scope_list),
        ).fetchall()
        return [r[0] for r in rows]

    def importers_repo(self, targets: Iterable[str]) -> list[str]:
        """Every file in the *whole repo* that imports any name in ``targets``.

        Layer C's reverse-dependency query: the whole-repo variant of
        :meth:`importers_within` with the ``scope_files`` clause dropped, so it
        surfaces importers the agent has never opened (mirrors CodeGraph's
        ``getDependentFilePaths``). Backed by ``idx_edges_target`` — a cheap
        DISTINCT scan even at repo scale.
        """
        target_list = list(targets)
        if not target_list:
            return []
        tq = ",".join("?" for _ in target_list)
        rows = self._conn.execute(
            f"SELECT DISTINCT source_file FROM edges WHERE kind = 'imports' AND target IN ({tq})",
            (*target_list,),
        ).fetchall()
        return [r[0] for r in rows]

    # -- files table (staleness) ---------------------------------------------

    def all_indexed_paths(self) -> list[str]:
        """Every path with a persisted files row (the indexed universe)."""
        rows = self._conn.execute("SELECT path FROM files").fetchall()
        return [r[0] for r in rows]

    def indexed_hashes(self) -> dict:
        """``{path: content_hash}`` for every indexed file."""
        rows = self._conn.execute("SELECT path, content_hash FROM files").fetchall()
        return {r[0]: r[1] for r in rows}

    def content_hash_of(self, path: str) -> str:
        """The stored content hash for a single ``path`` ("" when not indexed).

        A single-row lookup for the one caller that needs one file's hash (F2's
        ``precise_callers`` cache key) — cheaper than :meth:`indexed_hashes`, and
        it reuses the hash the extractor already computed at parse time instead of
        re-reading + re-hashing the file.
        """
        row = self._conn.execute("SELECT content_hash FROM files WHERE path = ?", (path,)).fetchone()
        return row[0] if row else ""

    def get_stale_paths(self, current: dict) -> list[str]:
        """Paths in ``current`` whose hash differs from stored (or untracked).

        ``current`` is ``{path: content_hash}`` of the live tree. A path is stale
        when it has no files row yet (new) or its stored hash no longer matches
        (changed) — the ``getStaleFiles`` equivalent that drives incremental
        re-parse. Unchanged files are skipped, so a warm store re-parses nothing.
        """
        stored = self.indexed_hashes()
        return [path for path, h in current.items() if stored.get(path) != h]


__all__ = ["CodeMapStore"]
