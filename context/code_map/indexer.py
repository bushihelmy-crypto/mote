"""RepoIndexer — a whole-repo, persistent reverse-dependency index (Layer C).

Where :class:`~mote.context.code_map.CodeMap` is *locality-scoped* (it only ever
holds files the agent touched), the indexer holds the **whole repo**: every
``.py`` file's import edges, so a file's reverse dependencies (``used by:``) can
name callers the agent never opened — mirroring CodeGraph's
``getDependentFilePaths``.

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
from typing import Iterable, Optional

from mote.common.logs import logger
from mote.common.text import content_hash
from mote.context.code_map import CodeMap
from mote.context.code_map.extractor import CodeMapExtractor
from mote.context.code_map.paths import codemap_db_path

#: Directory names pruned from the whole-repo walk (never indexed).
_SKIP_DIRS = {".git", ".mote", "__pycache__", ".venv", "venv", "node_modules", ".mypy_cache", ".pytest_cache"}


class RepoIndexer:
    """Whole-repo import graph over a persistent :class:`CodeMap`.

    Construct with the repo root; the DB path is derived per-repo under
    ``~/.mote``. :meth:`scan_all` does the cold full scan (stale-diff, so a warm
    store re-parses nothing); :meth:`refresh` re-parses just the given changed
    paths; :meth:`importers` answers the whole-repo reverse-dep query.
    """

    def __init__(self, repo_root: str, *, store_path: Optional[str] = None) -> None:
        self._repo_root = os.path.abspath(repo_root)
        db = store_path if store_path is not None else codemap_db_path(self._repo_root)
        self._map = CodeMap(db)
        # A dedicated extractor for content hashing (independent of the map's own
        # mtime-cached extractor, since the scan drives its own staleness diff).
        self._hasher = CodeMapExtractor()
        self._lock = threading.Lock()

    def close(self) -> None:
        self._map.close()

    # -- reverse-dep query (the injected callable) ---------------------------

    def importers(self, candidates: Iterable[str]) -> list:
        """Whole-repo files importing any of *candidates* (module-name spellings).

        The duck-typed callable :class:`CodeMapContextSource` injects into
        ``CodeMap.neighborhood`` as ``repo_importers``. Best-effort: a store
        error yields no importers rather than breaking the map.
        """
        try:
            with self._lock:
                return self._map._store.importers_repo(set(candidates))
        except Exception as exc:  # noqa: BLE001 — never break the map render
            logger.debug(f"RepoIndexer: importers query failed: {exc}")
            return []

    # -- scanning ------------------------------------------------------------

    def scan_all(self) -> None:
        """Full cold scan: index every stale/new ``.py``, drop vanished rows.

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
                stale = self._map._store.get_stale_paths(current)
                indexed = set(self._map._store.all_indexed_paths())
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"RepoIndexer: staleness diff failed: {exc}")
                return
            for path in stale:
                self._reparse(path)
            # Drop rows for files that vanished since the last scan.
            for gone in indexed - set(current):
                try:
                    self._map._store.delete_file(gone)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"RepoIndexer: delete of vanished {gone} failed: {exc}")

    def refresh(self, paths: Iterable[str]) -> None:
        """Incremental re-parse of just the given changed ``.py`` paths.

        Fed by the roles-layer FileChanged hook. A path that no longer exists is
        deleted from the index; a non-``.py`` path is ignored. Best-effort.
        """
        with self._lock:
            for raw in paths:
                path = os.path.abspath(raw)
                if not path.endswith(".py"):
                    continue
                if os.path.exists(path):
                    self._reparse(path)
                else:
                    try:
                        self._map._store.delete_file(path)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(f"RepoIndexer: delete of {path} failed: {exc}")

    # -- internals -----------------------------------------------------------

    def _reparse(self, path: str) -> None:
        """Extract + upsert one file (assumes the write lock is held)."""
        try:
            extract = self._hasher.extract(path)
            self._map._store.upsert_file(extract)
        except Exception as exc:  # noqa: BLE001 — one bad file must not abort the scan
            logger.debug(f"RepoIndexer: reparse of {path} failed: {exc}")

    def _current_hashes(self) -> dict:
        """``{abspath: content_hash}`` for every ``.py`` under the repo root."""
        current: dict = {}
        for dirpath, dirnames, filenames in os.walk(self._repo_root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fname in filenames:
                if not fname.endswith(".py"):
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
