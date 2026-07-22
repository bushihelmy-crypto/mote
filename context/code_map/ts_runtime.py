"""tree-sitter runtime — the ONE import-guarded loader for every native grammar.

This is the single file in CodeMap that imports tree-sitter. Everything else
(the generic builder, the tree-sitter provider, the registry) asks *this* module
whether the runtime is present and borrows a parser through it, so the whole map
degrades cleanly to Python-only when the native grammars are not installed: an
absent :mod:`tree_sitter_language_pack` leaves :func:`available` ``False`` and
:func:`parser_for` returning ``None``, and the provider registry simply never
appends the tree-sitter languages.

Parsers are cached **per thread** (:class:`threading.local`): a
``tree_sitter.Parser`` is not safe to share across threads, and the indexer scans
the repo on a thread pool, so each thread gets its own parser per grammar. An
unknown grammar name (or a grammar the pack cannot build) yields ``None`` and is
remembered as absent so the failing lookup is not retried every call.
"""

from __future__ import annotations

import threading
from typing import Any, Optional

try:  # The one guarded import. Absent pack / failed native build → Python-only.
    from tree_sitter_language_pack import get_parser, has_language

    _AVAILABLE = True
except Exception:  # noqa: BLE001 — any import/build failure degrades to Python-only
    _AVAILABLE = False

    def has_language(name: str) -> bool:  # type: ignore[misc]
        return False

    def get_parser(name: str) -> Any:  # type: ignore[misc]
        # Annotated ``-> Any`` (not the inferred ``NoReturn`` of a pure-raise body)
        # so the real ``get_parser`` from the pack — which returns a ``Parser`` — is
        # assignable to this fallback's type in the guarded-import union.
        raise RuntimeError("tree-sitter runtime unavailable")


# Per-thread parser cache: {grammar_name: Parser}. Parser is not thread-safe, so
# each thread keeps its own; the pack's Language objects are shared safely.
_local = threading.local()

# Grammar names proven absent/unbuildable this process — never retried.
_missing: set[str] = set()
_missing_lock = threading.Lock()


def available() -> bool:
    """True when the tree-sitter runtime imported (native grammars are usable)."""
    return _AVAILABLE


def has_grammar(name: str) -> bool:
    """True when *name* is a grammar the runtime can load (and not proven absent)."""
    if not _AVAILABLE or name in _missing:
        return False
    try:
        return bool(has_language(name))
    except Exception:  # noqa: BLE001 — treat a probe failure as absent
        return False


def parser_for(name: str) -> Optional[Any]:
    """A thread-local ``tree_sitter.Parser`` for grammar *name*, or ``None``.

    Returns ``None`` (never raises) when the runtime is absent or the grammar
    cannot be built; a failed grammar is remembered so it is not retried.
    """
    if not _AVAILABLE or name in _missing:
        return None
    cache: dict[str, Any] = getattr(_local, "parsers", None)  # type: ignore[assignment]
    if cache is None:
        cache = {}
        _local.parsers = cache
    parser = cache.get(name)
    if parser is not None:
        return parser
    try:
        parser = get_parser(name)
    except Exception:  # noqa: BLE001 — unknown / unbuildable grammar → absent
        with _missing_lock:
            _missing.add(name)
        return None
    cache[name] = parser
    return parser


def parse(name: str, source: str) -> Optional[Any]:
    """Parse *source* with grammar *name*, returning the tree's root node or None."""
    parser = parser_for(name)
    if parser is None:
        return None
    try:
        tree = parser.parse(source.encode("utf-8"))
    except Exception:  # noqa: BLE001 — a parser blow-up must not break the map
        return None
    return tree.root_node


__all__ = ["available", "has_grammar", "parser_for", "parse"]
