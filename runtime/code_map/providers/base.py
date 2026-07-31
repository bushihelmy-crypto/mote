"""Language-provider seam used by every supported language.

CodeMap's structure half is a *dispatch* facade: one :class:`LanguageProvider`
per language turns a file's source into the neutral
:class:`~mote.runtime.code_map.model` rows + a resolved
:class:`~mote.runtime.code_map.scopes.ScopeGraph`, and one :class:`ModuleResolver`
per language answers the module→file / file→module questions the facade needs to
draw cross-file edges. Everything above the provider (store, facade, indexer) is
language-agnostic; everything language-specific lives behind these two protocols.

- :class:`LanguageProvider` — parse one file. Returns a :class:`ProviderExtract`
  (the parsed structure) or ``None`` when the source cannot be parsed. It never
  reads the file or touches mtimes — the facade owns freshness + I/O.
- :class:`ModuleResolver` — the (stateless) module-name arithmetic: which repo
  dirs anchor an absolute import (``import_roots``), a dotted/relative module →
  a file on disk (``module_to_path``), a file → the module spellings it could be
  imported by (``module_candidates``), and whether a spelling is relative
  (``is_relative``). Grouping the touched set by resolver keeps a ``.py`` from
  ever trying to resolve a ``.go`` import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

from mote.runtime.code_map.model import ImportBinding, ImportRef, Symbol
from mote.runtime.code_map.scopes import ScopeGraph


@dataclass
class ProviderExtract:
    """The parsed structure a provider derives from one file (no I/O concerns).

    The facade wraps this into a :class:`~mote.runtime.code_map.model.FileExtract`,
    adding the path, content hash, language, and the shared call-edge post-step.
    """

    module_summary: str = ""
    symbols: list[Symbol] = field(default_factory=list)
    scope_graph: Optional[ScopeGraph] = None
    imports: list[str] = field(default_factory=list)
    import_refs: list[ImportRef] = field(default_factory=list)
    import_bindings: list[ImportBinding] = field(default_factory=list)


@runtime_checkable
class ModuleResolver(Protocol):
    """Stateless module-name arithmetic for one language (module ⇄ file)."""

    def import_roots(self, abs_files: list[str]) -> set[str]:
        """Directory anchors under which an absolute import maps to a file."""
        ...

    def module_to_path(self, module: str, roots: set[str]) -> Optional[str]:
        """Map ``module`` to a repo file under one of ``roots`` (or ``None``)."""
        ...

    def module_candidates(self, abspath: str) -> set[str]:
        """Module-name spellings ``abspath`` could be imported by."""
        ...

    def is_relative(self, module: str) -> bool:
        """True when ``module`` is a relative import spelling (unanchored)."""
        ...


@runtime_checkable
class LanguageProvider(Protocol):
    """Parse one file of a single language into the neutral extract model."""

    @property
    def language(self) -> str:
        """Stable language id (e.g. ``"python"``, ``"go"``)."""
        ...

    @property
    def extensions(self) -> tuple[str, ...]:
        """File extensions this provider claims (each incl. the leading dot)."""
        ...

    def extract_tree(self, source: str, abspath: str) -> Optional[ProviderExtract]:
        """Parse ``source`` (already read for ``abspath``) or ``None`` on failure."""
        ...

    def module_resolver(self) -> ModuleResolver:
        """This language's module ⇄ file resolver."""
        ...


__all__ = ["ProviderExtract", "ModuleResolver", "LanguageProvider"]
