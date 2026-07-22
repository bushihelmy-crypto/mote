"""RepoIndexer — a whole-repo, persistent reverse-dependency index (Layer C).

Where :class:`~mote.context.code_map.CodeMap` is *locality-scoped* (it only ever
holds files the agent touched), the indexer holds the **whole repo**: every
indexable source file's import edges (every extension a registered provider
claims — ``.py`` always, plus the tree-sitter languages when available), so a
file's reverse dependencies (``used by:``) can name callers the agent never
opened — mirroring CodeGraph's ``getDependentFilePaths``.

It owns its own persistent :class:`CodeMap` (a SQLite DB under ``~/.mote``, see
:func:`~mote.context.code_map.paths.codemap_db_path`) so a warm start skips the
full rescan (content-hash staleness diff). The cold scan runs off the event loop
(the caller kicks it via ``run_in_executor``) so the ~hundreds-of-ms walk never
blocks a turn; a ``threading.Lock`` guards the SQLite writes (the store is opened
``check_same_thread=False``).

Layering: this lives in the low ``context`` layer and names nothing above it.
The change *signal* (a file was edited) is fed in from the ``roles`` layer via
:meth:`refresh` (wired to the reused ``environment.watching`` FileWatcher there),
and the reverse-dep *query* is exposed as :meth:`importers` — a duck-typed
callable the touched-set source injects into ``CodeMap.neighborhood``.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Iterable, Optional

from mote.common.logs import logger
from mote.common.text import content_hash
from mote.context.code_map import CodeMap
from mote.context.code_map.extractor import CodeMapExtractor
from mote.context.code_map.languages import registered_extensions
from mote.context.code_map.model import Symbol
from mote.context.code_map.paths import codemap_db_path

#: Directory names pruned from the whole-repo walk (never indexed). Covers VCS /
#: Python caches plus the build/dependency output dirs of the tree-sitter
#: languages (JS ``node_modules``, Go/Rust ``target``/``vendor``, Java
#: ``.gradle``, C/C++ ``build``/``cmake-build-debug``, C# ``bin``/``obj``, …) —
#: parsing generated / vendored code would only add noise edges.
_SKIP_DIRS = {
    ".git",
    ".mote",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    "target",
    "bin",
    "obj",
    "dist",
    "build",
    "out",
    ".gradle",
    ".idea",
    "vendor",
    "Pods",
    "DerivedData",
    "cmake-build-debug",
}


class RepoIndexer:
    """Whole-repo import graph over a persistent :class:`CodeMap`.

    Construct with the repo root; the DB path is derived per-repo under
    ``~/.mote``. :meth:`scan_all` does the cold full scan (stale-diff, so a warm
    store re-parses nothing); :meth:`refresh` re-parses just the given changed
    paths; :meth:`importers` answers the whole-repo reverse-dep query, and
    :meth:`symbols_in` / :meth:`module_summary_of` expose the whole-repo symbol
    table + module purpose so a dangling-import target can be resolved LSP-free.
    :meth:`references_to` / :meth:`definition_of` add the symbol-precise
    find-references / go-to-definition surface (Decision C) off the same store.
    """

    def __init__(self, repo_root: str, *, store_path: Optional[str] = None) -> None:
        self._repo_root = os.path.abspath(repo_root)
        self._store_path = store_path if store_path is not None else codemap_db_path(self._repo_root)
        self._map: CodeMap | None = None
        # A dedicated extractor for content hashing (independent of the map's own
        # mtime-cached extractor, since the scan drives its own staleness diff).
        self._hasher = CodeMapExtractor()
        self._lock = threading.Lock()

    def close(self) -> None:
        if self._map is not None:
            self._map.close()
            self._map = None

    def prepare(self) -> CodeMap:
        """Open the persistent store exactly once, at the first real operation."""
        if self._map is None:
            if self._store_path != ":memory:":
                Path(self._store_path).parent.mkdir(parents=True, exist_ok=True)
            self._map = CodeMap(self._store_path)
        return self._map

    # -- reverse-dep query (the injected callable) ---------------------------

    def importers(self, candidates: Iterable[str]) -> list:
        """Whole-repo files importing any of *candidates* (module-name spellings).

        The duck-typed callable :class:`CodeMapContextSource` injects into
        ``CodeMap.neighborhood`` as ``repo_importers``. Best-effort: a store
        error yields no importers rather than breaking the map.
        """
        try:
            with self._lock:
                return self.prepare()._store.importers_repo(set(candidates))
        except Exception as exc:  # noqa: BLE001 — never break the map render
            logger.debug(f"RepoIndexer: importers query failed: {exc}")
            return []

    def symbols_in(self, path: str) -> list:
        """Whole-repo symbols defined in *path*, from the persistent index.

        The LSP-free source of a dangling-import target's "defines" view: the
        cold ``scan_all`` already extracted every repo file's symbols, so an
        *untouched* imported file's public surface is answerable without an LSP.
        Best-effort — a store error yields no symbols rather than breaking the
        map. Same shape as :meth:`importers`.
        """
        try:
            with self._lock:
                return self.prepare()._store.symbols_in(path)
        except Exception as exc:  # noqa: BLE001 — never break the map render
            logger.debug(f"RepoIndexer: symbols_in query failed: {exc}")
            return []

    def module_summary_of(self, path: str) -> str:
        """Whole-repo module-docstring summary for *path*, from the index.

        The LSP-free source of a dangling-import target's *purpose* line. Best-
        effort — a store error yields ``""`` rather than breaking the map.
        """
        try:
            with self._lock:
                return self.prepare()._store.module_summary_of(path)
        except Exception as exc:  # noqa: BLE001 — never break the map render
            logger.debug(f"RepoIndexer: module_summary_of query failed: {exc}")
            return ""

    # -- whole-repo navigation (Decision C, exposed off the cold store) ------

    def references_to(self, path: str, symbol: str) -> list:
        """Whole-repo ``(path, line)`` uses of ``symbol`` defined in ``path``.

        The symbol-precise reverse-dep query: the passive source points a
        dangling target's ``used by:`` at this (falling back to the coarse
        :meth:`importers` when empty). Reads the cold store the cold ``scan_all``
        already populated — no re-parse. Best-effort — a store error yields ``[]``
        rather than breaking the map. Same shape as :meth:`importers`.
        """
        try:
            with self._lock:
                return self.prepare().references_to(path, symbol)
        except Exception as exc:  # noqa: BLE001 — never break the map render
            logger.debug(f"RepoIndexer: references_to query failed: {exc}")
            return []

    def definition_of(self, name: str, module: str) -> Optional[Symbol]:
        """The whole-repo :class:`Symbol` a ``module``.``name`` import points at.

        Maps the dotted ``module`` to a repo file (anchored at the repo root) and
        returns that file's top-level function/class named ``name`` — the LSP-free
        go-to-definition a future navigation tool consumes. ``None`` when the
        module maps to no repo file or the name is not an exported top-level
        symbol. Best-effort — a store error yields ``None``.
        """
        try:
            with self._lock:
                code_map = self.prepare()
                target = code_map._module_to_path_any(module, {self._repo_root})
                if target is None:
                    return None
                for sym in code_map._store.symbols_in(target):
                    if sym.qualified_name == name and sym.kind in ("function", "class"):
                        return sym
                return None
        except Exception as exc:  # noqa: BLE001 — never break the map render
            logger.debug(f"RepoIndexer: definition_of query failed: {exc}")
            return None

    # -- scanning ------------------------------------------------------------

    def scan_all(self) -> None:
        """Full cold scan: index every stale/new indexable file, drop vanished rows.

        Walks the repo, computes each file's content hash, asks the store which
        are stale (hash differs or untracked), re-parses only those, and deletes
        rows for files that no longer exist. Best-effort — a single unreadable
        file is skipped, never raised.
        """
        try:
            current = self._current_hashes()
        except Exception as exc:  # noqa: BLE001 — a walk failure is non-fatal
            logger.debug(f"RepoIndexer: scan walk failed: {exc}")
            return
        with self._lock:
            try:
                code_map = self.prepare()
                stale = code_map._store.get_stale_paths(current)
                indexed = set(code_map._store.all_indexed_paths())
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"RepoIndexer: staleness diff failed: {exc}")
                return
            for path in stale:
                self._reparse(path)
            # Drop rows for files that vanished since the last scan.
            for gone in indexed - set(current):
                try:
                    code_map._store.delete_file(gone)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"RepoIndexer: delete of vanished {gone} failed: {exc}")

    def refresh(self, paths: Iterable[str]) -> None:
        """Incremental re-parse of just the given changed indexable paths.

        Fed by the roles-layer FileChanged hook. A path that no longer exists is
        deleted from the index; a path whose extension no registered provider
        claims is ignored. Best-effort.
        """
        extensions = registered_extensions()
        with self._lock:
            for raw in paths:
                path = os.path.abspath(raw)
                if os.path.splitext(path)[1] not in extensions:
                    continue
                if os.path.exists(path):
                    self._reparse(path)
                else:
                    try:
                        self.prepare()._store.delete_file(path)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(f"RepoIndexer: delete of {path} failed: {exc}")

    # -- internals -----------------------------------------------------------

    def _reparse(self, path: str) -> None:
        """Extract + upsert one file (assumes the write lock is held)."""
        try:
            extract = self._hasher.extract(path)
            self.prepare()._store.upsert_file(extract)
        except Exception as exc:  # noqa: BLE001 — one bad file must not abort the scan
            logger.debug(f"RepoIndexer: reparse of {path} failed: {exc}")

    def _current_hashes(self) -> dict:
        """``{abspath: content_hash}`` for every indexable file under the repo root.

        "Indexable" = any extension some registered provider claims. With
        tree-sitter absent this is exactly ``{".py"}`` (identical to the former
        Python-only walk); with it present the walk also picks up JS/TS/Go/Rust/
        Java/C#/C/C++ sources.
        """
        current: dict = {}
        extensions = registered_extensions()
        for dirpath, dirnames, filenames in os.walk(self._repo_root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fname in filenames:
                if os.path.splitext(fname)[1] not in extensions:
                    continue
                full = os.path.join(dirpath, fname)
                h = self._hash_file(full)
                if h is not None:
                    current[full] = h
        return current

    @staticmethod
    def _hash_file(path: str) -> Optional[str]:
        # Routes through the shared ``content_hash`` so the staleness diff uses
        # byte-identical inputs to what CodeMapExtractor persisted — otherwise
        # every scan would see every file as stale.
        try:
            with open(path, "r", encoding="utf-8") as f:
                source = f.read()
        except (OSError, UnicodeDecodeError):
            return None
        return content_hash(source)


__all__ = ["RepoIndexer"]
