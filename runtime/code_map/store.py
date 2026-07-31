"""CodeMapStore — persist :class:`FileExtract` rows into a SQLite graph.

The store is the *persistence* half of the local code map. It holds two families
of tables, kept deliberately separate because they answer different questions:

**Presentation** (what a file looks like — drives the neighborhood render):

- ``nodes`` — one row per :class:`Symbol` (function / method / class);
- ``imports`` — one row per module name a file imports (file → module);
- ``calls`` — one row per intra-file call edge (qualified caller → bare callee).

**Resolution** (the persisted scope graph — drives LSP-free name resolution):

- ``scopes`` / ``defs`` / ``refs`` — the resolved :class:`ScopeGraph` of a file,
  each ref carrying its *resolved* local target (or NULL when external), so
  whole-repo go-to-definition / find-references work with no re-parse and no LSP;
- ``import_bindings`` — the symbol-level cross-file seam: ``local_name`` in this
  file = ``imported_name`` from ``module`` (so a reverse-dep query can bind an
  imported name to the real ``def`` in its defining file, not just a file edge).

The split is deliberate: ``nodes`` is the *presentation* symbol table (public
functions/classes only), while ``defs`` is *resolution*-scoped (includes params,
locals, imports, global/nonlocal decls). Presentation reads ``nodes``; resolution
reads ``defs``/``refs``/``scopes``.

Deliberately *not* CodeGraph's schema: no FTS5, no visibility / decorators /
language columns. Reverse-dependency queries are answered coarsely by matching
import *module* names, and precisely by matching ``import_bindings`` at the
*symbol* level, against candidates the facade computes from a file's dotted path.

Idempotent by file: :meth:`upsert_file` deletes every prior row for a path and
re-inserts, so re-parsing a changed file never accumulates stale rows. The
connection is opened ``check_same_thread=False`` — all use is single-thread
today, but the flag keeps a stray event-loop-thread access from raising.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Iterable, Optional

from mote.runtime.code_map.model import CallEdge, FileExtract, ImportBinding, Symbol
from mote.runtime.code_map.scopes import Def, Ref, Scope, ScopeGraph

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS nodes (
    file_path TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    signature TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_nodes_file ON nodes(file_path);

CREATE TABLE IF NOT EXISTS imports (
    source_file TEXT NOT NULL,
    module TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_imports_source ON imports(source_file);
CREATE INDEX IF NOT EXISTS idx_imports_module ON imports(module);

CREATE TABLE IF NOT EXISTS calls (
    source_file TEXT NOT NULL,
    caller TEXT NOT NULL DEFAULT '',
    callee TEXT NOT NULL,
    line INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_calls_source ON calls(source_file);

CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    indexed_at INTEGER,
    module_summary TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    skip_class_scope INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS scopes (
    file_path TEXT NOT NULL,
    scope_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    parent_id INTEGER,
    start_line INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scopes_file ON scopes(file_path);

CREATE TABLE IF NOT EXISTS defs (
    file_path TEXT NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL DEFAULT '',
    scope_id INTEGER NOT NULL,
    line INTEGER NOT NULL,
    kind TEXT NOT NULL,
    is_global INTEGER NOT NULL DEFAULT 0,
    is_nonlocal INTEGER NOT NULL DEFAULT 0,
    body_scope INTEGER
);
CREATE INDEX IF NOT EXISTS idx_defs_file ON defs(file_path);
CREATE INDEX IF NOT EXISTS idx_defs_name ON defs(file_path, name);

CREATE TABLE IF NOT EXISTS refs (
    file_path TEXT NOT NULL,
    name TEXT NOT NULL,
    scope_id INTEGER NOT NULL,
    line INTEGER NOT NULL,
    col INTEGER NOT NULL,
    is_call INTEGER NOT NULL DEFAULT 0,
    via_self INTEGER NOT NULL DEFAULT 0,
    resolved_scope_id INTEGER,
    resolved_line INTEGER
);
CREATE INDEX IF NOT EXISTS idx_refs_file ON refs(file_path);
CREATE INDEX IF NOT EXISTS idx_refs_resolved ON refs(file_path, resolved_scope_id, resolved_line);

CREATE TABLE IF NOT EXISTS import_bindings (
    file_path TEXT NOT NULL,
    local_name TEXT NOT NULL,
    module TEXT NOT NULL,
    imported_name TEXT NOT NULL DEFAULT '',
    line INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_import_bindings_file ON import_bindings(file_path);
CREATE INDEX IF NOT EXISTS idx_import_bindings_symbol ON import_bindings(module, imported_name);
"""
_SCHEMA_VERSION = 1


class CodeMapStore:
    """A tiny SQLite store for the local code map's presentation + resolution.

    Pass ``":memory:"`` (the default) for an ephemeral per-session map, or a file
    path to persist across sessions. The schema is created on construction.
    """

    def __init__(self, path: str = ":memory:") -> None:
        if path != ":memory:" and Path(path).exists() and not self._is_current(path):
            Path(path).unlink()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        if not self._conn.execute("SELECT version FROM schema_meta").fetchone():
            self._conn.execute("INSERT INTO schema_meta (version) VALUES (?)", (_SCHEMA_VERSION,))
        self._conn.commit()

    @staticmethod
    def _is_current(path: str) -> bool:
        connection = sqlite3.connect(path)
        try:
            row = connection.execute("SELECT version FROM schema_meta").fetchone()
            return row == (_SCHEMA_VERSION,)
        except sqlite3.OperationalError:
            return False
        finally:
            connection.close()

    def close(self) -> None:
        self._conn.close()

    # -- writes --------------------------------------------------------------

    def upsert_file(self, extract: FileExtract) -> None:
        """Replace all rows for ``extract.path`` with its current parse.

        Idempotent: a prior parse of the same file is fully cleared first (across
        every table), so a re-parse after an edit never leaves stale rows behind.
        The presentation rows (``nodes`` / ``imports`` / ``calls``) come straight
        from the extract; the resolution rows (``scopes`` / ``defs`` / ``refs``)
        are written only when a resolved :class:`ScopeGraph` is attached, and each
        ref is stored with its resolved *local* target (or NULL when external).
        """
        path = extract.path
        cur = self._conn.cursor()
        self._clear_file(cur, path)

        cur.executemany(
            "INSERT INTO nodes (file_path, name, qualified_name, kind, start_line, signature, summary) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    path,
                    s.name,
                    s.qualified_name,
                    s.kind,
                    s.start_line,
                    s.signature,
                    s.summary,
                )
                for s in extract.symbols
            ],
        )
        cur.executemany(
            "INSERT INTO imports (source_file, module) VALUES (?, ?)",
            [(path, module) for module in extract.imports],
        )
        cur.executemany(
            "INSERT INTO calls (source_file, caller, callee, line) VALUES (?, ?, ?, ?)",
            [(path, c.caller, c.callee, c.line) for c in extract.calls],
        )
        cur.executemany(
            "INSERT INTO import_bindings (file_path, local_name, module, imported_name, line) VALUES (?, ?, ?, ?, ?)",
            [(path, b.local_name, b.module, b.imported_name, b.line) for b in extract.import_bindings],
        )
        self._write_graph(cur, path, extract.scope_graph)

        # The files row is the staleness anchor: content hash + parse time +
        # language + the resolver's class-skip policy (so ``graph_of`` rehydrates
        # a graph with the same LEGB behavior the provider built). An
        # INSERT-OR-REPLACE keyed on the path keeps exactly one row per file.
        skip_class = 1 if (extract.scope_graph is None or extract.scope_graph.skip_class_scope) else 0
        cur.execute(
            "INSERT OR REPLACE INTO files "
            "(path, content_hash, indexed_at, module_summary, language, skip_class_scope) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                path,
                extract.content_hash,
                int(time.time()),
                extract.module_summary,
                extract.language,
                skip_class,
            ),
        )
        self._conn.commit()

    def _write_graph(self, cur: sqlite3.Cursor, path: str, graph: Optional[ScopeGraph]) -> None:
        """Persist the resolved scope graph for ``path`` (no-op when absent)."""
        if graph is None:
            return
        cur.executemany(
            "INSERT INTO scopes (file_path, scope_id, kind, parent_id, start_line) VALUES (?, ?, ?, ?, ?)",
            [(path, s.id, s.kind, s.parent, s.start_line) for s in graph.scopes.values()],
        )
        cur.executemany(
            "INSERT INTO defs (file_path, name, qualified_name, scope_id, line, kind, is_global, is_nonlocal, body_scope) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    path,
                    d.name,
                    d.qualified_name,
                    d.scope,
                    d.line,
                    d.kind,
                    int(d.is_global),
                    int(d.is_nonlocal),
                    d.body_scope,
                )
                for d in graph.defs
            ],
        )
        ref_rows = []
        for ref in graph.refs:
            target = graph.resolve(ref)
            rsid = target.scope if target is not None else None
            rline = target.line if target is not None else None
            ref_rows.append(
                (
                    path,
                    ref.name,
                    ref.scope,
                    ref.line,
                    ref.col,
                    int(ref.is_call),
                    int(ref.via_self),
                    rsid,
                    rline,
                )
            )
        cur.executemany(
            "INSERT INTO refs (file_path, name, scope_id, line, col, is_call, via_self, resolved_scope_id, resolved_line) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ref_rows,
        )

    def delete_file(self, path: str) -> None:
        """Drop all rows for ``path`` (a vanished file). Idempotent."""
        cur = self._conn.cursor()
        self._clear_file(cur, path)
        cur.execute("DELETE FROM files WHERE path = ?", (path,))
        self._conn.commit()

    @staticmethod
    def _clear_file(cur: sqlite3.Cursor, path: str) -> None:
        """Delete every presentation + resolution row for ``path`` (not files)."""
        cur.execute("DELETE FROM nodes WHERE file_path = ?", (path,))
        cur.execute("DELETE FROM imports WHERE source_file = ?", (path,))
        cur.execute("DELETE FROM calls WHERE source_file = ?", (path,))
        cur.execute("DELETE FROM scopes WHERE file_path = ?", (path,))
        cur.execute("DELETE FROM defs WHERE file_path = ?", (path,))
        cur.execute("DELETE FROM refs WHERE file_path = ?", (path,))
        cur.execute("DELETE FROM import_bindings WHERE file_path = ?", (path,))

    # -- presentation reads --------------------------------------------------

    def symbols_in(self, file_path: str) -> list[Symbol]:
        """Every symbol defined in ``file_path``, ordered by definition line."""
        rows = self._conn.execute(
            "SELECT name, qualified_name, kind, start_line, signature, summary FROM nodes "
            "WHERE file_path = ? ORDER BY start_line",
            (file_path,),
        ).fetchall()
        return [
            Symbol(
                name=r[0],
                qualified_name=r[1],
                kind=r[2],
                start_line=r[3],
                signature=r[4],
                summary=r[5],
            )
            for r in rows
        ]

    def module_summary_of(self, file_path: str) -> str:
        """The stored module-docstring summary for ``file_path`` ("" when none)."""
        row = self._conn.execute("SELECT module_summary FROM files WHERE path = ?", (file_path,)).fetchone()
        return row[0] if row and row[0] else ""

    def calls_in(self, file_path: str) -> list[CallEdge]:
        """Every intra-file call edge recorded for ``file_path``, in call order."""
        rows = self._conn.execute(
            "SELECT caller, callee, line FROM calls WHERE source_file = ? ORDER BY line",
            (file_path,),
        ).fetchall()
        return [CallEdge(caller=r[0], callee=r[1], line=r[2]) for r in rows]

    def imports_of(self, file_path: str) -> list[str]:
        """Module names ``file_path`` imports (order preserved, as inserted)."""
        rows = self._conn.execute(
            "SELECT module FROM imports WHERE source_file = ? ORDER BY rowid",
            (file_path,),
        ).fetchall()
        return [r[0] for r in rows]

    def importers_within(self, targets: Iterable[str], scope_files: Iterable[str]) -> list[str]:
        """Files in ``scope_files`` that import any name in ``targets``.

        The coarse (module-level) reverse-dependency query. ``targets`` are the
        module names by which a file could be imported (the facade computes these
        from the file's dotted path); ``scope_files`` is the touched set, so we
        never report importers outside the locality the agent is working in.
        """
        target_list = list(targets)
        scope_list = list(scope_files)
        if not target_list or not scope_list:
            return []
        tq = ",".join("?" for _ in target_list)
        sq = ",".join("?" for _ in scope_list)
        rows = self._conn.execute(
            f"SELECT DISTINCT source_file FROM imports " f"WHERE module IN ({tq}) AND source_file IN ({sq})",
            (*target_list, *scope_list),
        ).fetchall()
        return [r[0] for r in rows]

    def importers_repo(self, targets: Iterable[str]) -> list[str]:
        """Every file in the *whole repo* that imports any name in ``targets``.

        Layer C's coarse reverse-dependency query: the whole-repo variant of
        :meth:`importers_within` with the ``scope_files`` clause dropped, so it
        surfaces importers the agent has never opened. Backed by
        ``idx_imports_module`` — a cheap DISTINCT scan even at repo scale.
        """
        target_list = list(targets)
        if not target_list:
            return []
        tq = ",".join("?" for _ in target_list)
        rows = self._conn.execute(
            f"SELECT DISTINCT source_file FROM imports WHERE module IN ({tq})",
            (*target_list,),
        ).fetchall()
        return [r[0] for r in rows]

    # -- resolution reads (the persisted scope graph) ------------------------

    def graph_of(self, file_path: str) -> ScopeGraph:
        """Reconstruct the persisted :class:`ScopeGraph` for ``file_path``.

        The round-trip of what :meth:`upsert_file` stored: scopes + defs + refs
        rehydrated into a resolver so a future navigation tool can walk the graph
        with no re-parse. An unindexed (or graph-less) file yields an empty graph.
        """
        scope_rows = self._conn.execute(
            "SELECT scope_id, kind, parent_id, start_line FROM scopes WHERE file_path = ?",
            (file_path,),
        ).fetchall()
        scopes = {r[0]: Scope(id=r[0], kind=r[1], parent=r[2], start_line=r[3]) for r in scope_rows}
        def_rows = self._conn.execute(
            "SELECT name, scope_id, line, kind, qualified_name, is_global, is_nonlocal, body_scope "
            "FROM defs WHERE file_path = ?",
            (file_path,),
        ).fetchall()
        defs = [
            Def(
                name=r[0],
                scope=r[1],
                line=r[2],
                kind=r[3],
                qualified_name=r[4],
                is_global=bool(r[5]),
                is_nonlocal=bool(r[6]),
                body_scope=r[7],
            )
            for r in def_rows
        ]
        ref_rows = self._conn.execute(
            "SELECT name, scope_id, line, col, is_call, via_self FROM refs WHERE file_path = ?",
            (file_path,),
        ).fetchall()
        refs = [
            Ref(
                name=r[0],
                scope=r[1],
                line=r[2],
                col=r[3],
                is_call=bool(r[4]),
                via_self=bool(r[5]),
            )
            for r in ref_rows
        ]
        srow = self._conn.execute("SELECT skip_class_scope FROM files WHERE path = ?", (file_path,)).fetchone()
        skip_class = bool(srow[0]) if srow is not None else True
        return ScopeGraph(scopes=scopes, defs=defs, refs=refs, skip_class_scope=skip_class)

    def import_bindings_of(self, file_path: str) -> list[ImportBinding]:
        """The symbol-level import bindings recorded for ``file_path``."""
        rows = self._conn.execute(
            "SELECT local_name, module, imported_name, line FROM import_bindings WHERE file_path = ? ORDER BY rowid",
            (file_path,),
        ).fetchall()
        return [ImportBinding(local_name=r[0], module=r[1], imported_name=r[2], line=r[3]) for r in rows]

    def definition_of(self, file_path: str, name: str) -> Optional[Def]:
        """The top-level (module-scope) function/class named ``name`` in ``file_path``.

        The intra-file half of go-to-definition: an imported name binds to a file
        (the facade resolves the module → path); this returns that file's exported
        definition of the name, or None. Module-scope only — a method (class scope)
        is not an importable top-level symbol.
        """
        row = self._conn.execute(
            "SELECT name, scope_id, line, kind, qualified_name, is_global, is_nonlocal, body_scope FROM defs "
            "WHERE file_path = ? AND name = ? AND kind IN ('function', 'class') "
            "AND is_global = 0 AND is_nonlocal = 0 "
            "AND scope_id IN (SELECT scope_id FROM scopes WHERE file_path = ? AND kind = 'module') "
            "ORDER BY line LIMIT 1",
            (file_path, name, file_path),
        ).fetchone()
        if row is None:
            return None
        return Def(
            name=row[0],
            scope=row[1],
            line=row[2],
            kind=row[3],
            qualified_name=row[4],
            is_global=bool(row[5]),
            is_nonlocal=bool(row[6]),
            body_scope=row[7],
        )

    def references_to(self, file_path: str, name: str) -> list[tuple[str, int]]:
        """Intra-file ``(file_path, line)`` uses of the top-level symbol ``name``.

        Every ref in ``file_path`` whose resolved local target is the module-scope
        def of ``name`` — the same-file half of find-references. Cross-file uses
        are answered by :meth:`symbol_importers` (the facade/indexer join the two).
        """
        d = self.definition_of(file_path, name)
        if d is None:
            return []
        rows = self._conn.execute(
            "SELECT line FROM refs WHERE file_path = ? AND resolved_scope_id = ? AND resolved_line = ? ORDER BY line",
            (file_path, d.scope, d.line),
        ).fetchall()
        return [(file_path, r[0]) for r in rows]

    def symbol_importers(self, modules: Iterable[str], imported_name: str) -> list[tuple[str, int]]:
        """``(file_path, line)`` for every file importing ``imported_name`` from ``modules``.

        The symbol-level (Decision B) reverse-dependency query: matches
        ``import_bindings`` on both the module *and* the specific imported symbol,
        so "who uses ``thing`` from ``pkg.other``" is answerable precisely — not
        merely "who imports ``pkg.other``". ``modules`` are the candidate dotted
        spellings the facade computes for the defining file.
        """
        module_list = list(modules)
        if not module_list or not imported_name:
            return []
        mq = ",".join("?" for _ in module_list)
        rows = self._conn.execute(
            f"SELECT DISTINCT file_path, line FROM import_bindings " f"WHERE imported_name = ? AND module IN ({mq})",
            (imported_name, *module_list),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

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
