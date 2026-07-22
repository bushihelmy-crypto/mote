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

Reverse-dependency resolution is deliberately heuristic and *per-language*. We
have no cross-file symbol resolver (an explicit non-goal); instead each file's
:class:`~mote.context.code_map.providers.base.ModuleResolver` (chosen by
extension) turns a path into the module names by which it could plausibly be
imported (Python ``a/b/c.py`` -> ``c``, ``b.c``, ``a.b.c`` ...), and
:meth:`CodeMapStore.importers_within` string-matches those against recorded
import targets. The touched set is grouped *by resolver* so a ``.py`` import
target is never matched against a ``.go`` file's candidates — cross-language
edges cannot be forged. This is a locality heuristic, not a compiler.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from mote.common.text import uri_to_path
from mote.context.code_map.extractor import CodeMapExtractor
from mote.context.code_map.languages import all_registered_providers, provider_for
from mote.context.code_map.model import CallEdge, FileExtract, Symbol
from mote.context.code_map.providers.base import ModuleResolver
from mote.context.code_map.scopes import Def
from mote.context.code_map.store import CodeMapStore

__all__ = [
    "CodeMap",
    "FileNeighborhood",
    "CodeMapExtractor",
    "CodeMapStore",
    "FileExtract",
    "Symbol",
    "CallEdge",
]


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
    # {unread_target_abspath: [symbol names]} — what a dangling import defines.
    # Baseline sourced LSP-free from the persistent whole-repo index; LSP (Layer
    # B) merges over it per target when present (more precise → wins).
    unread_symbols: dict[str, list[str]] = field(default_factory=dict)
    # Opt B: {unread_target_abspath: module-docstring first line} — the dangling
    # import's *purpose*, from the whole-repo index (LSP-free).
    unread_module_summary: dict[str, str] = field(default_factory=dict)
    # Opt A: {unread_target_abspath: [whole-repo importer abspaths]} — who else
    # depends on the dangling import, from the whole-repo index (LSP-free).
    unread_imported_by: dict[str, list[str]] = field(default_factory=dict)


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

        # Group the touched set BY resolver (=by language). Import edges are only
        # ever drawn within a group, so a ``.py`` never resolves against a ``.go``.
        groups = self._group_by_resolver(abs_files)

        # Per-group: the candidate index (module spelling -> touched files that
        # offer it) and the import roots, each computed by that group's own
        # resolver. ``candidates_by_file`` stays flat (used for the reverse query).
        candidates_by_file: dict[str, set[str]] = {}
        # f -> (its resolver, its group's candidate index, its group's roots).
        context_of: dict[str, tuple[ModuleResolver, dict[str, list[str]], set[str]]] = {}
        for resolver, members in groups:
            file_by_candidate: dict[str, list[str]] = {}
            for f in members:
                cands = resolver.module_candidates(f)
                candidates_by_file[f] = cands
                for c in cands:
                    file_by_candidate.setdefault(c, []).append(f)
            roots = resolver.import_roots(members)
            for f in members:
                context_of[f] = (resolver, file_by_candidate, roots)

        scope = set(abs_files)
        out: list[FileNeighborhood] = []
        for f in abs_files:
            if f not in context_of:
                # Unknown language — listed (so the caller sees the full touched
                # set) but structure-less, exactly as a non-Python path was before.
                out.append(FileNeighborhood(path=f))
                continue
            resolver, file_by_candidate, roots = context_of[f]
            symbols = self._store.symbols_in(f)
            module_summary = self._store.module_summary_of(f)
            calls = self._store.calls_in(f)
            # Forward: which touched files does f import? Match f's import targets
            # against the module candidates of every other touched file in-group.
            raw_imports = self._store.imports_of(f)
            imports = self._resolve_imports_within(raw_imports, file_by_candidate, exclude=f)
            # Dangling: internal repo files f imports that are NOT in the touched
            # set (nor already a within-set edge) — the "you haven't opened these
            # yet" hint. stdlib / third-party targets map to nothing and drop out.
            unread = self._dangling_imports(raw_imports, roots, resolver, within=file_by_candidate, exclude_paths=scope)
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

    # -- unified navigation API (Decision C) ---------------------------------
    #
    # The LSP-free go-to-definition / find-references surface, built purely off
    # the persisted scope graph + symbol-level import bindings. A future
    # navigation tool consumes these unchanged; the passive context source uses
    # ``references_to`` for symbol-precise reverse-deps. All three are cheap cold
    # reads (no re-parse) and best-effort (an unindexed file resolves to nothing).

    def resolve_import(self, local_name: str, importer_path: str) -> Optional[tuple[str, str]]:
        """Where ``local_name`` (as bound in ``importer_path``) is imported from.

        Reads ``importer_path``'s persisted :class:`ImportBinding`s for the one
        binding ``local_name`` and maps its dotted ``module`` back to a repo file
        on disk (via the importer's own package roots). Returns
        ``(target_path, imported_name)`` — ``imported_name`` is the symbol pulled
        by a ``from module import name`` (``""`` for a plain ``import a.b.c``,
        where the local name binds the module itself). ``None`` when the name is
        not an import, or its module maps to no file (stdlib / third-party).
        """
        importer = os.path.abspath(importer_path)
        resolver = self._resolver_for(importer)
        if resolver is None:
            return None
        binding = next(
            (b for b in self._store.import_bindings_of(importer) if b.local_name == local_name),
            None,
        )
        if binding is None:
            return None
        target = resolver.module_to_path(binding.module, resolver.import_roots([importer]))
        if target is None:
            return None
        return (target, binding.imported_name)

    def definition_of(self, name: str, *, in_file: Optional[str] = None) -> Optional[Def]:
        """The :class:`Def` ``name`` resolves to when used in ``in_file`` (or None).

        Intra-file first: a module-scope ``def``/``class`` named ``name`` in
        ``in_file`` wins. Otherwise cross-file: if ``name`` is an import in
        ``in_file``, follow :meth:`resolve_import` to its defining file and return
        that file's exported definition. ``in_file`` is required (a bare name has
        no resolution context); returns None when nothing binds it.
        """
        if in_file is None:
            return None
        in_file = os.path.abspath(in_file)
        local = self._store.definition_of(in_file, name)
        if local is not None:
            return local
        resolved = self.resolve_import(name, in_file)
        if resolved is None:
            return None
        target, symbol = resolved
        return self._store.definition_of(target, symbol or name)

    def references_to(self, target_path: str, symbol: str) -> list[tuple[str, int]]:
        """Whole-repo ``(path, line)`` uses of ``symbol`` defined in ``target_path``.

        Two precise sources joined, no LSP: the *intra-file* true use sites (refs
        whose resolved local target is the module-scope def of ``symbol``) plus the
        *cross-file* import sites (files whose ``import_bindings`` pull ``symbol``
        from one of ``target_path``'s module candidates). The target's own path is
        never listed as a cross-file importer.
        """
        target = os.path.abspath(target_path)
        refs: list[tuple[str, int]] = list(self._store.references_to(target, symbol))
        for path, line in self._store.symbol_importers(self._candidates(target), symbol):
            if path != target:
                refs.append((path, line))
        return refs

    # -- LSP-free unread resolution from the whole-repo index ----------------

    def resolve_unread_from_index(
        self,
        unread_paths: list[str],
        *,
        symbols_of: Callable[[str], list],
        module_summary_of: Callable[[str], str],
        importers_of: Callable[[set], list],
        references_of: Optional[Callable[[str, str], list]] = None,
    ) -> tuple[dict[str, list[str]], dict[str, str], dict[str, list[str]]]:
        """Resolve each dangling-import target from the persistent index, LSP-free.

        The whole-repo cold scan already extracted every ``.py``'s symbols,
        module docstring, and import edges, so an *untouched* imported file's
        public surface / purpose / reverse-deps are answerable without any LSP.
        For each target: ``symbols`` = its public top-level symbol names (capped
        at :data:`_UNREAD_SYMBOL_CAP`); ``summary`` = its module purpose (Opt B);
        ``used_by`` = the whole-repo files depending on it, minus the target
        itself (Opt A).

        Decision B (symbol-level used-by): when a ``references_of(target, name)``
        reader is wired, ``used_by`` is the union of the files that *reference*
        the target's public symbols — who uses the file's API, not merely who
        imports its module. Falls back to the coarse module-name ``importers_of``
        when no references reader is given or the symbol query surfaces nobody.

        Every input is a duck-typed callable (the :class:`RepoIndexer` readers),
        so this low ``context`` layer names nothing above it. Best-effort per
        target — a raising reader for one target skips it, never breaks the map.
        """
        symbols: dict[str, list[str]] = {}
        summaries: dict[str, str] = {}
        imported_by: dict[str, list[str]] = {}
        for target in unread_paths:
            try:
                names = self._public_top_level_names(symbols_of(target))[: self._UNREAD_SYMBOL_CAP]
                if names:
                    symbols[target] = names
                summary = module_summary_of(target)
                if summary:
                    summaries[target] = summary
                importers: list[str] = []
                if references_of is not None and names:
                    importers = self._symbol_level_importers(target, names, references_of)
                if not importers:
                    importers = [p for p in importers_of(self._candidates(target)) if p != target]
                if importers:
                    imported_by[target] = sorted(set(importers))
            except Exception:  # noqa: BLE001 — a bad target must not break the map
                continue
        return symbols, summaries, imported_by

    @staticmethod
    def _symbol_level_importers(target: str, names: list[str], references_of: Callable[[str, str], list]) -> list[str]:
        """Files referencing any public symbol of *target* (symbol-precise used-by).

        Unions ``references_of(target, name)`` over the target's public top-level
        names, dropping the intra-file sites (the target itself). Decision B: this
        answers "who uses this file's API", where the coarse module-name query
        only answers "who imports this module".
        """
        importers: set[str] = set()
        for name in names:
            for path, _line in references_of(target, name) or []:
                if path != target:
                    importers.add(path)
        return sorted(importers)

    @staticmethod
    def _public_top_level_names(symbols: list) -> list[str]:
        """Public top-level names from a :class:`Symbol` list (dedup, order kept).

        A caller imports a file's *public top-level surface* — a def/class whose
        name is public (no leading ``_``) and is not nested (no dotted
        ``qualified_name``). Methods/closures/privates are dropped, mirroring the
        risk-label's ``_is_public_interface`` filter but over the index's
        :class:`Symbol` rows rather than LSP dicts.
        """
        names: list[str] = []
        seen: set[str] = set()
        for s in symbols or []:
            qn = getattr(s, "qualified_name", "") or ""
            name = getattr(s, "name", "") or ""
            if not name or "." in qn or name.startswith("_"):
                continue
            if name not in seen:
                seen.add(name)
                names.append(name)
        return names

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

    # -- per-file resolver routing -------------------------------------------
    #
    # module⇄file arithmetic is language-specific, so every such call routes
    # through the file's own :class:`ModuleResolver` (picked by extension). A
    # ``.py`` and a ``.go`` never share a resolver, so their candidate spellings
    # can't collide into a forged edge. Python-only environments resolve exactly
    # one resolver, byte-for-byte identical to the former inline statics.

    @staticmethod
    def _resolver_for(abspath: str) -> Optional[ModuleResolver]:
        """The :class:`ModuleResolver` for *abspath*'s language (or None if unknown)."""
        provider = provider_for(abspath)
        return provider.module_resolver() if provider is not None else None

    @staticmethod
    def _candidates(abspath: str) -> set[str]:
        """Import-name candidates for *abspath* via its resolver (empty if unknown)."""
        resolver = CodeMap._resolver_for(abspath)
        return resolver.module_candidates(abspath) if resolver is not None else set()

    @staticmethod
    def _group_by_resolver(abs_files: list[str]) -> list[tuple[ModuleResolver, list[str]]]:
        """Partition *abs_files* by their language resolver (identity-grouped).

        Files of unknown languages (no provider) are dropped — the caller lists
        them structure-less. Grouping is by resolver identity so all files of one
        language share a single candidate index + root set; import edges are only
        ever drawn within a group, never across languages.
        """
        groups: list[tuple[ModuleResolver, list[str]]] = []
        by_id: dict[int, tuple[ModuleResolver, list[str]]] = {}
        for f in abs_files:
            resolver = CodeMap._resolver_for(f)
            if resolver is None:
                continue  # unknown language — no resolver, listed structure-less
            entry = by_id.get(id(resolver))
            if entry is None:
                entry = (resolver, [])
                by_id[id(resolver)] = entry
                groups.append(entry)
            entry[1].append(f)
        return groups

    @staticmethod
    def _dangling_imports(
        raw_imports: list[str],
        roots: set[str],
        resolver: ModuleResolver,
        *,
        within: dict[str, list[str]],
        exclude_paths: set[str],
    ) -> set[str]:
        """Repo files a file imports that are NOT in the touched set (abspaths).

        Classifies each import target three ways via *resolver*: already a
        within-set edge (skip, it's in ``imports``); maps to a real repo file but
        is not touched (a *dangling* internal edge — the return value); or maps to
        nothing on disk (stdlib / third-party — dropped). Relative imports are
        skipped: without the importer's package context they cannot be anchored
        reliably, and their in-set cases are already covered by
        :meth:`_resolve_imports_within`.
        """
        hit: set[str] = set()
        for target in raw_imports:
            if resolver.is_relative(target):
                continue  # relative import — no reliable absolute anchor
            if target in within:
                continue  # already a within-set edge (dedup)
            path = resolver.module_to_path(target, roots)
            if path and path not in exclude_paths:
                hit.add(path)
        return hit

    @staticmethod
    def _module_to_path_any(module: str, roots: set[str]) -> Optional[str]:
        """First repo file *module* maps to across all registered resolvers (or None).

        The indexer's whole-repo definition lookup is language-agnostic — it does
        not know which language defines ``module`` — so it tries every registered
        resolver and takes the first hit. Python-only environments iterate exactly
        one resolver, identical to the former direct ``_module_to_path`` call.
        """
        for provider in all_registered_providers():
            path = provider.module_resolver().module_to_path(module, roots)
            if path is not None:
                return path
        return None
