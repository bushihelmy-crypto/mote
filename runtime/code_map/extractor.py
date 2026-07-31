"""CodeMapExtractor — language-agnostic dispatch facade over providers.

The extractor owns exactly the two things that are *not* language-specific: file
I/O + freshness (an ``{path: mtime_ns}`` cache so :meth:`needs_refresh` can skip
unchanged files) and the shared post-parse assembly. Everything about *parsing*
a given language lives behind a
:class:`~mote.runtime.code_map.providers.base.LanguageProvider`; the facade picks
one by extension (:func:`~mote.runtime.code_map.languages.provider_for`), hands it
the already-read source, and wraps the neutral
:class:`~mote.runtime.code_map.providers.base.ProviderExtract` it returns into a
:class:`~mote.runtime.code_map.model.FileExtract` — adding the path, content hash,
language tag, and the one shared call-edge step (derived from the resolved
:class:`~mote.runtime.code_map.scopes.ScopeGraph`, identical for every language).

Best-effort throughout: an unreadable file, an unknown extension, or a provider
that fails to parse all yield an empty (or structure-less) :class:`FileExtract`
and are *never* raised — a code map that silently omits a file it could not parse
is correct; one that breaks a turn is not.

"""

from __future__ import annotations

import os
from typing import Optional

from mote.runtime.code_map.languages import provider_for
from mote.runtime.code_map.model import CallEdge, FileExtract
from mote.runtime.code_map.scopes import ScopeGraph
from mote.runtime.content_hashing import content_hash as _content_hash
from mote.runtime.persistence import mtime_ns

_MAX_SOURCE_LINE_CHARS = 1000


class CodeMapExtractor:
    """Dispatches per-file parsing to a language provider, with an mtime cache."""

    def __init__(self) -> None:
        # abspath -> mtime_ns at last successful (or attempted) parse. Used by
        # needs_refresh to decide whether a re-parse is needed.
        self._mtime: dict[str, int] = {}

    def needs_refresh(self, path: str) -> bool:
        """True if *path* changed on disk since we last parsed it (or never did)."""
        current = mtime_ns(path)
        if current is None:
            return False  # gone / unreadable — nothing to refresh
        return self._mtime.get(os.path.abspath(path)) != current

    def extract(self, path: str) -> FileExtract:
        """Parse *path* and record its mtime. Best-effort — empty extract on failure.

        Always stamps the mtime cache (even on failure) so a broken or unsupported
        file is not re-parsed every turn until it changes again. The extension
        picks the provider; an unknown extension yields a bare (structure-less)
        extract, exactly as a non-Python path did before the seam existed.
        """
        abspath = os.path.abspath(path)
        current = mtime_ns(abspath)
        if current is not None:
            self._mtime[abspath] = current

        source = self._read(abspath)
        if source is None:
            return FileExtract(path=abspath)

        content_hash = _content_hash(source)

        provider = provider_for(abspath)
        if provider is None:
            # Unknown language — record the version (stable hash) but no structure.
            return FileExtract(path=abspath, content_hash=content_hash)

        if provider.language != "python" and any(len(line) > _MAX_SOURCE_LINE_CHARS for line in source.splitlines()):
            return FileExtract(
                path=abspath,
                content_hash=content_hash,
                language=provider.language,
            )

        tree = provider.extract_tree(source, abspath)
        if tree is None:
            # A broken file still gets a stable content hash + its language tag so
            # the store's staleness diff sees it as parsed-at-this-version.
            return FileExtract(path=abspath, content_hash=content_hash, language=provider.language)

        extract = FileExtract(path=abspath, content_hash=content_hash, language=provider.language)
        extract.module_summary = tree.module_summary
        extract.symbols = tree.symbols
        extract.scope_graph = tree.scope_graph
        # Intra-file call edges come from the scope-aware resolver — one shared
        # step, identical for every language.
        extract.calls = self._call_edges(tree.scope_graph)
        extract.imports = tree.imports
        extract.import_refs = tree.import_refs
        extract.import_bindings = tree.import_bindings
        return extract

    # -- shared post-parse assembly ------------------------------------------

    @staticmethod
    def _call_edges(graph: Optional[ScopeGraph]) -> list[CallEdge]:
        """Turn resolver call edges into :class:`CallEdge` rows (bare callee name)."""
        if graph is None:
            return []
        edges: list[CallEdge] = []
        for owner, callee, line in graph.call_edges():
            caller = owner.qualified_name if owner is not None else ""
            edges.append(CallEdge(caller=caller, callee=callee.name, line=line))
        return edges

    @staticmethod
    def _read(path: str) -> Optional[str]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except (OSError, UnicodeDecodeError):
            return None


__all__ = ["CodeMapExtractor"]
