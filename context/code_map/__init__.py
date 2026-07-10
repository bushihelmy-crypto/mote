"""CodeMap — the facade that turns touched files into a local navigation map.

This package is the *self-built navigation layer* distilled from CodeGraph: not a
whole-repo knowledge graph, but a locality-scoped one that only ever holds files
the agent has actually worked with. It composes the two halves:

- :class:`~metagpt.context.code_map.extractor.CodeMapExtractor` — ``ast``-derived
  symbols + import/call edges for one Python file, with an mtime freshness cache;
- :class:`~metagpt.context.code_map.store.CodeMapStore` — a tiny SQLite nodes /
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
from dataclasses import dataclass, field

from metagpt.context.code_map.extractor import CallEdge, CodeMapExtractor, FileExtract, Symbol
from metagpt.context.code_map.store import CodeMapStore

__all__ = ["CodeMap", "FileNeighborhood", "CodeMapExtractor", "CodeMapStore", "FileExtract", "Symbol", "CallEdge"]


@dataclass
class FileNeighborhood:
    """One touched file's local structure + its edges *within the touched set*."""

    path: str  # absolute path
    symbols: list[Symbol] = field(default_factory=list)  # what this file defines
    imports: list[str] = field(default_factory=list)  # touched files it imports (abspaths)
    imported_by: list[str] = field(default_factory=list)  # touched files importing it (abspaths)


class CodeMap:
    """Composes extractor + store into lazy-refresh + touched-set neighborhood.

    Owns one :class:`CodeMapExtractor` (holding the mtime cache) and one
    :class:`CodeMapStore` (``:memory:`` by default — the map lives only as long as
    the session unless a path is supplied).
    """

    def __init__(self, store_path: str = ":memory:") -> None:
        self._extractor = CodeMapExtractor()
        self._store = CodeMapStore(store_path)

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

    def neighborhood(self, files: list[str]) -> list[FileNeighborhood]:
        """Local dependency picture for each *files* entry, clamped to *files*.

        For every file: the symbols it defines, which of the other touched files
        it imports, and which of them import it. Files are refreshed first, so the
        picture reflects the current on-disk state. Only ``.py`` files carry
        structure; others yield an empty neighborhood (still listed, so the caller
        sees the full touched set).
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

        scope = set(abs_files)
        out: list[FileNeighborhood] = []
        for f in abs_files:
            symbols = self._store.symbols_in(f)
            # Forward: which touched files does f import? Match f's import targets
            # against the module candidates of every other touched file.
            raw_imports = self._store.imports_of(f)
            imports = self._resolve_imports_within(raw_imports, file_by_candidate, exclude=f)
            # Reverse: which touched files import f? Ask the store for scope files
            # whose import target hits any of f's own module candidates.
            imported_by = [
                imp
                for imp in self._store.importers_within(candidates_by_file[f], scope)
                if imp != f
            ]
            out.append(
                FileNeighborhood(
                    path=f,
                    symbols=symbols,
                    imports=sorted(imports),
                    imported_by=sorted(imported_by),
                )
            )
        return out

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
