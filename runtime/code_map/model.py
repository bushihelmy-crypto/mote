"""Neutral extract data model shared by every language provider.

These dataclasses are the contract between a language *provider* (Python ``ast``,
tree-sitter, …) and the store / facade. They carry no ``ast`` (nor tree-sitter)
dependency — a provider parses a file into these rows and the rest of CodeMap
never learns which language produced them.

The multi-language provider seam (:mod:`~mote.runtime.code_map.providers`) and
the store import this authoritative neutral model without importing the
Python-specific extractor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from mote.runtime.code_map.scopes import ScopeGraph

#: Cap on a rendered doc/signature summary — one short line of intent, not prose.
#: Shared by every provider so the ``ast`` and tree-sitter walks trim alike.
SUMMARY_MAX_CHARS = 100


@dataclass(frozen=True)
class Symbol:
    """A definition site in a file: a function, method, class, or type.

    ``kind`` is presentation-only and free-form across languages —
    ``function`` | ``method`` | ``class`` | ``struct`` | ``interface`` | ``enum`` |
    ``impl`` | ``trait`` | ``namespace`` | … — never switched on by resolution.
    """

    name: str  # bare name, e.g. "foo" or "method"
    qualified_name: str  # dotted within-file path, e.g. "Baz.method"
    kind: str  # presentation label (see class docstring)
    start_line: int  # 1-based
    signature: str = ""  # params (+ return) for funcs/methods; "" for classes
    summary: str = ""  # docstring first line (intent), "" when undocumented


@dataclass(frozen=True)
class CallEdge:
    """An intra-file call: ``caller`` (qualified) invokes same-file ``callee``."""

    caller: str  # qualified name of the enclosing symbol, or "" at module level
    callee: str  # bare name of the called symbol (defined in this file)
    line: int  # 1-based call site


@dataclass(frozen=True)
class ImportRef:
    """A single imported binding + the source position that names it.

    Carries the *binding* (``thing`` in ``from pkg.other import thing``), the
    original ``imported_name`` in the source module (``thing``; "" for a whole-
    module ``import``), and the ``(line, col)`` of the reference site.
    """

    module: str  # dotted import target (leading dots for relative imports)
    name: str  # the imported binding at this site (asname or original)
    line: int  # 1-based
    col: int  # 0-based (LSP-style character offset)
    imported_name: str = ""  # original name in the module ("" for whole-module import)


@dataclass(frozen=True)
class ImportBinding:
    """Symbol-level cross-file seam: ``local_name`` == ``imported_name`` @ ``module``.

    The persisted form of a ``from module import imported_name as local_name``
    binding (``imported_name`` == "" for a whole-module ``import``), so a
    reverse-dep query can match "who imports symbol *X* from module *Y*" at the
    symbol level rather than only by module name.
    """

    local_name: str
    module: str
    imported_name: str
    line: int


@dataclass
class FileExtract:
    """Everything a provider derived from one file."""

    path: str  # absolute path
    module_summary: str = ""  # module docstring first line (intent), "" when undocumented
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)  # imported module names
    calls: list[CallEdge] = field(default_factory=list)  # intra-file symbol->symbol
    # Import *reference sites* (binding + position) — parallel to ``imports`` but
    # richer. NOT persisted in the store (Layer B resolves these live at render);
    # rides on the in-memory extract only.
    import_refs: list[ImportRef] = field(default_factory=list)
    # Symbol-level import bindings (persisted): the cross-file resolution seam.
    import_bindings: list[ImportBinding] = field(default_factory=list)
    # The resolved scope graph (persisted): scopes/defs/refs for whole-repo
    # find-references / go-to-definition without an LSP. ``None`` when the file
    # could not be parsed.
    scope_graph: Optional[ScopeGraph] = None
    # sha256 hex of the parsed source bytes ("" when unreadable). Drives the
    # persistent store's staleness diff (content-hash incremental re-parse).
    content_hash: str = ""
    # The language a provider identified for this file (e.g. "python", "go"),
    # "" when unknown / unparsed. Persisted so a mixed-language store round-trips.
    language: str = ""


__all__ = [
    "Symbol",
    "CallEdge",
    "ImportRef",
    "ImportBinding",
    "FileExtract",
    "SUMMARY_MAX_CHARS",
]
