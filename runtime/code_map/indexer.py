"""RepoIndexer — a whole-repo, persistent reverse-dependency index.

Where :class:`~mote.runtime.code_map.CodeMap` is *locality-scoped* (it only ever
holds files the agent touched), the indexer holds the **whole repo**: every
indexable source file's import edges (every extension a registered provider
claims — ``.py`` always, plus the tree-sitter languages when available), so a
file's reverse dependencies (``used by:``) can name callers the agent never
opened — mirroring CodeGraph's ``getDependentFilePaths``.

It owns its own persistent :class:`CodeMap` at an injected store path so a warm start skips the
full rescan (content-hash staleness diff). Runtime uses the dedicated-worker
``scan_all_async`` path, which keeps parsing and SQLite work off the event loop
and joins in-flight work on cancellation. A ``threading.Lock`` still guards
SQLite access for explicit synchronous callers (the store is opened
``check_same_thread=False``).

Layering: this lives in the low ``context`` layer and names nothing above it.
The change *signal* (a file was edited) is fed in from the ``roles`` layer via
:meth:`refresh` (wired to the reused ``environment.watching`` FileWatcher there),
and the reverse-dep *query* is exposed as :meth:`importers` — a duck-typed
callable the touched-set source injects into ``CodeMap.neighborhood``.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable, Optional, TypeVar

from mote.contracts.ports.code_intelligence.code_map import CodeReference, CodeSymbol
from mote.runtime.code_map import CodeMap
from mote.runtime.code_map.extractor import CodeMapExtractor
from mote.runtime.code_map.model import Symbol
from mote.runtime.content_hashing import content_hash
from mote.runtime.telemetry.logging import logger

#: Directory names pruned from the whole-repo walk (never indexed). Covers VCS /
#: Python caches plus the build/dependency output dirs of the tree-sitter
#: languages (JS ``node_modules``, Go/Rust ``target``/``vendor``, Java
#: ``.gradle``, C/C++ ``build``/``cmake-build-debug``, C# ``bin``/``obj``, …) —
#: parsing generated / vendored code would only add noise edges.
_T = TypeVar("_T")


class RepoIndexer:
    """Whole-repo import graph over a persistent :class:`CodeMap`.

        Construct with explicit repository, store, language, and path policy.
        :meth:`scan_all` does the cold full scan (stale-diff, so a warm
        store re-parses nothing); :meth:`refresh` re-parses just the given changed
        paths; :meth:`importers` answers the whole-repo reverse-dep query, and
        :meth:`symbols_in` / :meth:`module_summary_of` expose the whole-repo symbol
        table + module purpose so a dangling-import target can be resolved LSP-free.
    :meth:`references_to` / :meth:`definition_of` add the symbol-precise
    find-references / go-to-definition surface (Decision C) off the same store.
    """

    def __init__(
        self,
        repo_root: str,
        *,
        store_path: str,
        enabled_extensions: set[str],
        excluded_directories: set[str],
    ) -> None:
        self._repo_root = os.path.abspath(repo_root)
        self._store_path = store_path
        self._enabled_extensions = frozenset(enabled_extensions)
        self._excluded_directories = frozenset(excluded_directories)
        self._map: CodeMap | None = None
        # A dedicated extractor for content hashing (independent of the map's own
        # mtime-cached extractor, since the scan drives its own staleness diff).
        self._hasher = CodeMapExtractor()
        self._worker_hasher = CodeMapExtractor()
        self._lock = threading.Lock()
        self._worker: ThreadPoolExecutor | None = None
        self._worker_map: CodeMap | None = None

    def close(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            try:
                worker.submit(self._close_worker_map).result()
            finally:
                worker.shutdown(wait=True)
        with self._lock:
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

    def importers(self, candidates: Iterable[str]) -> tuple[str, ...]:
        """Whole-repo files importing any of *candidates* (module-name spellings).

        The duck-typed callable :class:`CodeMapContextSource` injects into
        ``CodeMap.neighborhood`` as ``repo_importers``. Best-effort: a store
        error yields no importers rather than breaking the map.
        """
        try:
            with self._lock:
                return tuple(sorted(self.prepare()._store.importers_repo(set(candidates))))
        except Exception as exc:  # noqa: BLE001 — never break the map render
            logger.debug(f"RepoIndexer: importers query failed: {exc}")
            return ()

    def symbols_in(self, path: str) -> tuple[CodeSymbol, ...]:
        """Whole-repo symbols defined in *path*, from the persistent index.

        The LSP-free source of a dangling-import target's "defines" view: the
        cold ``scan_all`` already extracted every repo file's symbols, so an
        *untouched* imported file's public surface is answerable without an LSP.
        Best-effort — a store error yields no symbols rather than breaking the
        map. Same shape as :meth:`importers`.
        """
        try:
            with self._lock:
                return tuple(
                    CodeSymbol(
                        symbol.name,
                        symbol.qualified_name,
                        symbol.kind,
                        symbol.start_line,
                        symbol.signature,
                        symbol.summary,
                    )
                    for symbol in sorted(
                        self.prepare()._store.symbols_in(path),
                        key=lambda item: (item.start_line, item.qualified_name, item.kind),
                    )
                )
        except Exception as exc:  # noqa: BLE001 — never break the map render
            logger.debug(f"RepoIndexer: symbols_in query failed: {exc}")
            return ()

    def module_summary_of(self, path: str) -> str | None:
        """Whole-repo module-docstring summary for *path*, from the index.

        The LSP-free source of a dangling-import target's *purpose* line. Best-
        effort — a store error yields ``""`` rather than breaking the map.
        """
        try:
            with self._lock:
                return self.prepare()._store.module_summary_of(path) or None
        except Exception as exc:  # noqa: BLE001 — never break the map render
            logger.debug(f"RepoIndexer: module_summary_of query failed: {exc}")
            return None

    # -- whole-repo navigation (Decision C, exposed off the cold store) ------

    def references_to(self, path: str, symbol: str) -> tuple[CodeReference, ...]:
        """Whole-repo ``(path, line)`` uses of ``symbol`` defined in ``path``.

        The symbol-precise reverse-dep query: the passive source points a
        dangling target's ``used by:`` at this (falling back to the coarse
        :meth:`importers` when empty). Reads the cold store the cold ``scan_all``
        already populated — no re-parse. Best-effort — a store error yields ``[]``
        rather than breaking the map. Same shape as :meth:`importers`.
        """
        try:
            with self._lock:
                return tuple(
                    CodeReference(reference_path, line)
                    for reference_path, line in sorted(
                        self.prepare().references_to(path, symbol),
                        key=lambda item: (item[0], item[1]),
                    )
                )
        except Exception as exc:  # noqa: BLE001 — never break the map render
            logger.debug(f"RepoIndexer: references_to query failed: {exc}")
            return ()

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

    async def scan_all_async(self) -> None:
        """Cancellation-safe off-loop cold scan for runtime startup.

        The synchronous variant remains for explicit offline/indexing callers.
        Runtime ownership uses this method so parsing and SQLite work cannot
        block the event loop, while cancellation still joins the current worker
        before returning.
        """
        try:
            current = await self._run_blocking(self._current_hashes)
        except Exception as exc:  # noqa: BLE001 — a walk failure is non-fatal
            logger.debug(f"RepoIndexer: scan walk failed: {exc}")
            return
        try:
            stale, gone = await self._run_blocking(self._scan_plan_worker, current)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"RepoIndexer: staleness diff failed: {exc}")
            return
        for path in stale:
            await self._run_blocking(self._reparse_worker, path)
        for path in gone:
            await self._run_blocking(self._delete_worker, path)

    async def refresh_async(self, paths: Iterable[str]) -> None:
        """Run an incremental refresh on the indexer's dedicated worker."""
        await self._run_blocking(self._refresh_worker, tuple(paths))

    def refresh(self, paths: Iterable[str]) -> None:
        """Incremental re-parse of just the given changed indexable paths.

        Fed by the roles-layer FileChanged hook. A path that no longer exists is
        deleted from the index; a path whose extension no registered provider
        claims is ignored. Best-effort.
        """
        extensions = self._enabled_extensions
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

    def _prepare_worker_map(self) -> CodeMap:
        if self._worker_map is None:
            if self._store_path != ":memory:":
                Path(self._store_path).parent.mkdir(parents=True, exist_ok=True)
            self._worker_map = CodeMap(self._store_path)
        return self._worker_map

    def _close_worker_map(self) -> None:
        if self._worker_map is not None:
            self._worker_map.close()
            self._worker_map = None

    def _scan_plan_worker(self, current: dict) -> tuple[list[str], set[str]]:
        code_map = self._prepare_worker_map()
        stale = code_map._store.get_stale_paths(current)
        indexed = set(code_map._store.all_indexed_paths())
        return stale, indexed - set(current)

    def _reparse_worker(self, path: str) -> None:
        try:
            extract = self._worker_hasher.extract(path)
            self._prepare_worker_map()._store.upsert_file(extract)
        except Exception as exc:  # noqa: BLE001 — one bad file must not abort the scan
            logger.debug(f"RepoIndexer: reparse of {path} failed: {exc}")

    def _delete_worker(self, path: str) -> None:
        try:
            self._prepare_worker_map()._store.delete_file(path)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"RepoIndexer: delete of vanished {path} failed: {exc}")

    def _refresh_worker(self, paths: tuple[str, ...]) -> None:
        extensions = self._enabled_extensions
        code_map = self._prepare_worker_map()
        for raw in paths:
            path = os.path.abspath(raw)
            if os.path.splitext(path)[1] not in extensions:
                continue
            if os.path.exists(path):
                self._reparse_worker(path)
            else:
                try:
                    code_map._store.delete_file(path)
                except Exception as exc:  # noqa: BLE001
                    logger.debug(f"RepoIndexer: delete of {path} failed: {exc}")

    async def _run_blocking(self, func: Callable[..., _T], *args: Any) -> _T:
        """Run all Runtime index work on one owned thread and join cancellation."""
        if self._worker is None:
            self._worker = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="mote-repo-index",
            )
        future = asyncio.get_running_loop().run_in_executor(
            self._worker,
            func,
            *args,
        )
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            await asyncio.gather(future, return_exceptions=True)
            raise

    def _current_hashes(self) -> dict:
        """``{abspath: content_hash}`` for every indexable file under the repo root.

        "Indexable" = any extension some registered provider claims. With
        tree-sitter absent this is exactly ``{".py"}`` (identical to the former
        Python-only walk); with it present the walk also picks up JS/TS/Go/Rust/
        Java/C#/C/C++ sources.
        """
        current: dict = {}
        extensions = self._enabled_extensions
        for dirpath, dirnames, filenames in os.walk(self._repo_root):
            dirnames[:] = [d for d in dirnames if d not in self._excluded_directories]
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
