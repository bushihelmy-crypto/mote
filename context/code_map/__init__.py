"""CodeMap — the facade that turns touched files into a local navigation map.

This package is the *self-built navigation layer* distilled from CodeGraph: not a
whole-repo knowledge graph, but a locality-scoped one that only ever holds files
the agent has actually worked with. It composes the two halves:

- :class:`~mote.context.code_map.extractor.CodeMapExtractor` — ``ast``-derived
  symbols + import/call edges for one Python file, with an mtime freshness cache;
- :class:`~mote.context.code_map.store.CodeMapStore` — a tiny SQLite nodes /
  edges store those extracts land in.

The facade adds the two operations a turn-context source actually needs:

- :meth:`ensure_fresh` — lazily (re)parse a path only when it changed on disk and
  land the result in the store. Idempotent and best-effort.
- :meth:`neighborhood` — given the touched set, return each file's *internal*
  dependency picture: what it defines, which touched files it imports, and which
  touched files import it (reverse deps). Everything is clamped to the touched
  set, so the map never leaks structure from files the agent has not opened.

Reverse-dependency resolution is deliberately heuristic. We have no cross-file
symbol resolver (an explicit non-goal); instead :meth:`_module_candidates` turns
a file path into the dotted module names by which it could plausibly be imported
(``a/b/c.py`` -> ``c``, ``b.c``, ``a.b.c`` and their relative-dot variants), and
:meth:`CodeMapStore.importers_within` string-matches those against recorded
import targets. This is a locality heuristic, not a compiler — good enough to say
"these touched files import this one" without a resolution pass.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from mote.common.text import uri_to_path
from mote.context.code_map.extractor import CallEdge, CodeMapExtractor, FileExtract, Symbol
from mote.context.code_map.store import CodeMapStore

_INIT = "__init__.py"

__all__ = ["CodeMap", "FileNeighborhood", "CodeMapExtractor", "CodeMapStore", "FileExtract", "Symbol", "CallEdge"]


@dataclass
class FileNeighborhood:
    """One touched file's local structure + its edges *within the touched set*."""

    path: str  # absolute path
    module_summary: str = ""  # module docstring first line (intent), "" when undocumented
    symbols: list[Symbol] = field(default_factory=list)  # what this file defines
    imports: list[str] = field(default_factory=list)  # touched files it imports (abspaths)
    imported_by: list[str] = field(default_factory=list)  # touched files importing it (abspaths)
    calls: list[CallEdge] = field(default_factory=list)  # intra-file symbol->symbol calls
    imports_unread: list[str] = field(default_factory=list)  # repo files imported but NOT touched (abspaths)
    # Layer B: {unread_target_abspath: [symbol names]} resolved live via LSP so
    # the model sees what a dangling import defines without opening the file.
    unread_symbols: dict[str, list[str]] = field(default_factory=dict)


class CodeMap:
    """Composes extractor + store into lazy-refresh + touched-set neighborhood.

    Owns one :class:`CodeMapExtractor` (holding the mtime cache) and one
    :class:`CodeMapStore` (``:memory:`` by default — the map lives only as long as
    the session unless a path is supplied).
    """

    #: How many top-level symbol names to surface per resolved unread target.
    _UNREAD_SYMBOL_CAP = 8
    #: Cap on distinct symbols we issue an LSP ``references`` query for per turn.
    #: Bounds the on-demand cost blow-up we rejected (a full-workspace scan) to a
    #: handful of the *interface-changed* symbols that actually put callers at risk.
    _MAX_REF_SYMBOLS = 5

    def __init__(self, store_path: str = ":memory:") -> None:
        self._extractor = CodeMapExtractor()
        self._store = CodeMapStore(store_path)
        # Layer B cache: (target_path, mtime_ns) -> resolved symbol names, so a
        # target is queried over LSP once per on-disk version (round-trips cost).
        self._unread_cache: dict[tuple, list[str]] = {}
        # F2 cache: (path, qualified_name, content_hash) -> [caller display paths],
        # so the same interface-changed symbol at the same file version is queried
        # over LSP once, not re-queried every turn until the file changes again.
        self._refs_cache: dict[tuple, list[str]] = {}

    def close(self) -> None:
        self._store.close()

    # -- ingest --------------------------------------------------------------

    def ensure_fresh(self, path: str) -> None:
        """(Re)parse *path* into the store only if it changed on disk since last.

        Best-effort: the extractor swallows unreadable / non-Python / syntactically
        broken files (empty extract) and stamps their mtime so they are not retried
        until they change again. Skips the store write when nothing changed.
        """
        if not self._extractor.needs_refresh(path):
            return
        extract = self._extractor.extract(path)
        self._store.upsert_file(extract)

    def ensure_all_fresh(self, paths: list[str]) -> None:
        """:meth:`ensure_fresh` for each path (order-preserving convenience)."""
        for p in paths:
            self.ensure_fresh(p)

    # -- query ---------------------------------------------------------------

    def neighborhood(
        self,
        files: list[str],
        *,
        repo_importers: Optional[Callable[[set], list]] = None,
    ) -> list[FileNeighborhood]:
        """Local dependency picture for each *files* entry, clamped to *files*.

        For every file: the symbols it defines, which of the other touched files
        it imports, and which of them import it. Files are refreshed first, so the
        picture reflects the current on-disk state. Only ``.py`` files carry
        structure; others yield an empty neighborhood (still listed, so the caller
        sees the full touched set).

        ``repo_importers`` (Layer C): when supplied, the reverse-dependency edge
        (``imported_by``) is sourced from it — the whole-repo importer set for a
        file's module candidates — instead of the touched-set-scoped query. It
        takes the set of module-name candidates and returns importer abspaths;
        the callee's own path is filtered out here. ``None`` keeps the query
        touched-set-only.
        """
        abs_files = [os.path.abspath(f) for f in files]
        self.ensure_all_fresh(abs_files)

        # Map each touched file to the module names it could be imported by, so a
        # recorded ``imports`` target string can be matched back to a touched file.
        candidates_by_file: dict[str, set[str]] = {f: self._module_candidates(f) for f in abs_files}
        # Reverse index: module-candidate -> touched files offering it.
        file_by_candidate: dict[str, list[str]] = {}
        for f, cands in candidates_by_file.items():
            for c in cands:
                file_by_candidate.setdefault(c, []).append(f)

        # Package roots the touched files sit under, so an absolute dotted import
        # (``a.b.c``) can be mapped back to a repo file on disk without a cwd or a
        # resolver — the anchors are inferred purely from the touched set. See
        # :meth:`_import_roots`.
        roots = self._import_roots(abs_files)

        scope = set(abs_files)
        out: list[FileNeighborhood] = []
        for f in abs_files:
            symbols = self._store.symbols_in(f)
            module_summary = self._store.module_summary_of(f)
            calls = self._store.calls_in(f)
            # Forward: which touched files does f import? Match f's import targets
            # against the module candidates of every other touched file.
            raw_imports = self._store.imports_of(f)
            imports = self._resolve_imports_within(raw_imports, file_by_candidate, exclude=f)
            # Dangling: internal repo files f imports that are NOT in the touched
            # set (nor already a within-set edge) — the "you haven't opened these
            # yet" hint. stdlib / third-party targets map to nothing and drop out.
            unread = self._dangling_imports(raw_imports, roots, within=file_by_candidate, exclude_paths=scope)
            # Reverse: which files import f? Layer C sources this from the whole
            # repo (repo_importers) when wired; otherwise the touched-set-scoped
            # query. Either way f's own path is excluded.
            if repo_importers is not None:
                importers = repo_importers(candidates_by_file[f])
            else:
                importers = self._store.importers_within(candidates_by_file[f], scope)
            imported_by = [imp for imp in importers if imp != f]
            out.append(
                FileNeighborhood(
                    path=f,
                    module_summary=module_summary,
                    symbols=symbols,
                    imports=sorted(imports),
                    imported_by=sorted(imported_by),
                    calls=calls,
                    imports_unread=sorted(unread),
                )
            )
        return out

    # -- Layer B: resolve dangling-import symbols via LSP --------------------

    async def resolve_unread(
        self,
        consumer_path: str,
        unread_paths: list[str],
        lsp_query,
    ) -> dict[str, list[str]]:
        """Resolve what each dangling-import *target* defines, via LSP.

        For each unread target, prefer ``documentSymbol`` (the file's top-level
        symbol table) for the "defines" view. Results are cached per
        ``(target, mtime_ns)`` so a target is queried once per on-disk version.
        ``lsp_query`` is the injected duck-typed facade with async
        ``document_symbols(path)`` / ``definition(path, line, char)``; anything
        missing/empty is omitted. Best-effort throughout — never raises.

        ``consumer_path`` is accepted for future definition-pointing (the
        importer whose bindings anchor a precise ``definition`` call); the
        current pass keys purely off the target's own symbol table.
        """
        if lsp_query is None:
            return {}
        resolved: dict[str, list[str]] = {}
        for target in unread_paths:
            try:
                names = await self._resolve_one_unread(target, lsp_query)
            except Exception:  # noqa: BLE001 — a bad target must not break the map
                names = []
            if names:
                resolved[target] = names
        return resolved

    async def _resolve_one_unread(self, target: str, lsp_query) -> list[str]:
        """documentSymbol names for one unread target, cached per version."""
        key = (target, self._mtime_ns(target))
        cached = self._unread_cache.get(key)
        if cached is not None:
            return cached
        symbols = await lsp_query.document_symbols(target)
        names = self._symbol_names(symbols)[: self._UNREAD_SYMBOL_CAP]
        # Evict this target's prior-version rows before storing the current one —
        # the version cache is only ever consulted at the *current* mtime, so old
        # rows are dead weight that would otherwise grow with every edit.
        self._evict_stale(self._unread_cache, identity=(target,), keep=key)
        self._unread_cache[key] = names
        return names

    # -- F2: on-demand precise callers of interface-changed symbols ----------

    async def precise_callers(
        self,
        path: str,
        symbols: list,
        lsp_query,
    ) -> dict[str, list[str]]:
        """Exact caller files for each *changed* symbol of *path*, via LSP references.

        For every :class:`Symbol` in *symbols* (capped at :data:`_MAX_REF_SYMBOLS`
        distinct names per turn), issue a single ``textDocument/references`` at the
        symbol's definition position and group the returned locations by
        referencing file — the precise call sites the string-index ``used by:``
        can only approximate. Returns ``{qualified_name: [caller_abspaths]}``; the
        callee's own file is filtered out. Cached per ``(path, qualified_name,
        content_hash)`` so the same edit is not re-queried across turns.

        Best-effort: ``lsp_query`` absent, any raise, or empty replies leave that
        symbol out of the result (``{}`` overall) — the row then falls back to the
        string-index ``used by:``.
        """
        if lsp_query is None or not symbols:
            return {}
        # Reuse the hash the extractor already computed + the store persisted at
        # parse time (``neighborhood`` refreshed the row this turn) — no re-read.
        content_hash = self._store.content_hash_of(os.path.abspath(path))
        out: dict[str, list[str]] = {}
        seen_names: set[str] = set()
        for sym in symbols:
            qname = getattr(sym, "qualified_name", None)
            if not qname or qname in seen_names:
                continue
            seen_names.add(qname)
            if len(seen_names) > self._MAX_REF_SYMBOLS:
                break
            try:
                callers = await self._callers_for_symbol(path, sym, content_hash, lsp_query)
            except Exception:  # noqa: BLE001 — a bad symbol must not break the map
                callers = []
            if callers:
                out[qname] = callers
        return out

    async def _callers_for_symbol(self, path: str, sym, content_hash: str, lsp_query) -> list[str]:
        """Caller files for one symbol, cached per ``(path, qualified_name, hash)``."""
        key = (path, getattr(sym, "qualified_name", ""), content_hash)
        cached = self._refs_cache.get(key)
        if cached is not None:
            return cached
        line = max(0, int(getattr(sym, "start_line", 1)) - 1)  # AST 1-based → LSP 0-based
        col = self._name_col(path, sym)
        locations = await lsp_query.references(path, line, col)
        callers = self._caller_files(locations, exclude=os.path.abspath(path))
        # Drop this symbol's rows at any *other* content hash — a re-edit changes
        # the hash, so stale-version entries are never read again.
        self._evict_stale(self._refs_cache, identity=(path, getattr(sym, "qualified_name", "")), keep=key)
        self._refs_cache[key] = callers
        return callers

    @staticmethod
    def _caller_files(locations, *, exclude: str) -> list[str]:
        """Unique referencing files from an LSP ``Location`` list (callee excluded)."""
        files: list[str] = []
        seen: set[str] = set()
        for loc in locations or []:
            if not isinstance(loc, dict):
                continue
            uri = loc.get("uri")
            if not uri:
                continue
            fpath = uri_to_path(uri)
            if fpath == exclude or fpath in seen:
                continue
            seen.add(fpath)
            files.append(fpath)
        return files

    @staticmethod
    def _name_col(path: str, sym) -> int:
        """0-based column of the symbol *name* on its definition line (best-effort).

        LSP wants the position *on the name* (``def foo`` → the ``f``), not the
        ``def`` keyword, so a ``references`` query resolves to the right symbol.
        Reads the file's start line, skips past the ``def`` / ``async def`` /
        ``class`` keyword, then locates the name as a whole word (guarding against
        a decorator or default-value substring on the same line matching first).
        Falls back to column 0 when the line can't be read or the name isn't found
        as a word after the keyword.
        """
        name = getattr(sym, "name", "") or ""
        start = int(getattr(sym, "start_line", 1))
        if not name or start < 1:
            return 0
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except (OSError, UnicodeDecodeError):
            return 0
        if start > len(lines):
            return 0
        line = lines[start - 1]
        # Anchor the search *after* the def/class keyword so a same-line decorator
        # or annotation containing the name can't win. ``re`` word boundary keeps
        # ``foo`` from matching inside ``foobar`` / ``_foo``.
        m = re.search(r"\b(?:async\s+def|def|class)\s+(" + re.escape(name) + r")\b", line)
        if m:
            return m.start(1)
        # No keyword match (unusual formatting) — fall back to a bare word match,
        # then to the leading column.
        m = re.search(r"\b" + re.escape(name) + r"\b", line)
        return m.start() if m else 0

    @staticmethod
    def _symbol_names(symbols) -> list[str]:
        """Top-level names from an LSP documentSymbol / SymbolInformation list.

        Handles both response shapes: hierarchical ``DocumentSymbol`` (``name``
        with nested ``children``) and flat ``SymbolInformation`` (``name`` +
        ``location``). Only top-level names are kept — the point is a compact
        "defines" line, not a full outline. Best-effort on malformed rows.
        """
        names: list[str] = []
        seen: set[str] = set()
        for sym in symbols or []:
            if not isinstance(sym, dict):
                continue
            name = sym.get("name")
            # Flat SymbolInformation carries containerName for nested symbols;
            # skip those to keep the line top-level.
            if not name or sym.get("containerName"):
                continue
            if name not in seen:
                seen.add(name)
                names.append(name)
        return names

    @staticmethod
    def _evict_stale(cache: dict, *, identity: tuple, keep) -> None:
        """Drop cache rows that share *identity*'s leading key but differ in version.

        Both LSP caches are keyed ``(*identity, version)`` — ``(target, mtime_ns)``
        or ``(path, qualified_name, content_hash)`` — and are only ever read at the
        *current* version. So when a file changes, its prior-version rows become
        unreachable dead weight that would otherwise accumulate one-per-edit for the
        session's life. Evicting the same-identity/other-version rows on each write
        bounds the cache to one live row per (target|symbol), keeping it O(touched
        symbols) instead of O(edits).
        """
        n = len(identity)
        stale = [k for k in cache if k != keep and k[:n] == identity]
        for k in stale:
            del cache[k]

    @staticmethod
    def _mtime_ns(path: str) -> int:
        try:
            return os.stat(path).st_mtime_ns
        except OSError:
            return 0

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _resolve_imports_within(
        raw_imports: list[str], file_by_candidate: dict[str, list[str]], *, exclude: str
    ) -> set[str]:
        """Touched files that *raw_imports* (a file's import targets) resolve to."""
        hit: set[str] = set()
        for target in raw_imports:
            key = target.lstrip(".")  # relative imports recorded with leading dots
            for f in file_by_candidate.get(key, ()):
                if f != exclude:
                    hit.add(f)
        return hit

    @staticmethod
    def _import_roots(abs_files: list[str]) -> set[str]:
        """Directory anchors under which an absolute dotted import maps to a file.

        A touched file at ``.../repo/pkg/sub/mod.py`` imported as ``pkg.sub.other``
        tells us ``.../repo`` is an import root: walking up past every package
        directory (those with an ``__init__.py``) lands on the path prefix that
        ``a.b.c`` -> ``a/b/c`` is relative to. We infer these purely from the
        touched set — no cwd, no ``sys.path`` — so mapping stays locality-driven.
        Non-package files (a bare script) anchor at their own directory.
        """
        roots: set[str] = set()
        for f in abs_files:
            if not f.endswith(".py"):
                continue
            d = os.path.dirname(f)
            # Climb out of the package chain: while the dir is itself a package,
            # its parent is the more-rooted anchor for a top-level dotted name.
            while os.path.exists(os.path.join(d, _INIT)):
                parent = os.path.dirname(d)
                if parent == d:
                    break
                d = parent
            roots.add(d)
        return roots

    @staticmethod
    def _dangling_imports(
        raw_imports: list[str],
        roots: set[str],
        *,
        within: dict[str, list[str]],
        exclude_paths: set[str],
    ) -> set[str]:
        """Repo files a file imports that are NOT in the touched set (abspaths).

        Classifies each import target three ways: already a within-set edge (skip,
        it's in ``imports``); maps to a real repo file under one of ``roots`` but
        is not touched (a *dangling* internal edge — the return value); or maps to
        nothing on disk (stdlib / third-party — dropped). Relative imports (leading
        dots) are skipped: without the importer's package context they cannot be
        anchored reliably, and their in-set cases are already covered by
        :meth:`_resolve_imports_within`.
        """
        hit: set[str] = set()
        for target in raw_imports:
            if target.startswith("."):
                continue  # relative import — no reliable absolute anchor
            if target in within:
                continue  # already a within-set edge (dedup)
            rel = target.replace(".", os.sep)
            for root in roots:
                mod = os.path.join(root, rel + ".py")
                pkg = os.path.join(root, rel, _INIT)
                path = mod if os.path.exists(mod) else (pkg if os.path.exists(pkg) else None)
                if path and path not in exclude_paths:
                    hit.add(path)
                    break  # first root that resolves wins
        return hit

    @staticmethod
    def _module_candidates(abspath: str) -> set[str]:
        """Dotted module names *abspath* could be imported by (locality heuristic).

        ``.../a/b/c.py`` -> {``c``, ``b.c``, ``a.b.c``}. A package ``__init__.py``
        also offers its parent-dir name (``.../pkg/__init__.py`` -> ``pkg`` ...).
        Not a resolver — just the plausible import spellings to string-match.
        """
        if not abspath.endswith(".py"):
            return set()
        directory, filename = os.path.split(abspath)
        stem = filename[:-3]  # drop ".py"
        parts = [p for p in directory.split(os.sep) if p]
        if stem == "__init__":
            # A package's importable name is its directory chain, not "__init__".
            segments = parts
        else:
            segments = parts + [stem]
        if not segments:
            return set()
        # Progressive right-anchored dotted suffixes: c, b.c, a.b.c ...
        cands: set[str] = set()
        for i in range(len(segments)):
            cands.add(".".join(segments[i:]))
        return cands
