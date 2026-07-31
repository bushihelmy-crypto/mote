"""Declarative language config contributed by tree-sitter languages.

A tree-sitter-backed language is *data*, not code: a :class:`LangConfig` names the
grammar, the node types that introduce definitions / scopes / calls, the receiver
tokens that make ``recv.m()`` a same-file method call, and the callables that pull
imports and answer module⇄file questions. :class:`~mote.runtime.code_map.generic_builder.TreeSitterBuilder`
reads exactly this to emit the same neutral
:class:`~mote.runtime.code_map.scopes.ScopeGraph` + :class:`~mote.runtime.code_map.model.Symbol`
rows the Python ``ast`` provider does — so every language reuses the one shared
resolver and the one shared call-edge step, contributing only this config.

The import extractor and module-resolver factory are held as *direct callables*
(not a string-keyed registry): the indirection a registry would add buys nothing
here — each language references its own extractor by name at config-build time —
and a direct callable is checked by the type system.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from mote.runtime.code_map.model import ImportBinding, ImportRef
from mote.runtime.code_map.providers.base import ModuleResolver

#: An import extractor: ``(root_node, source_bytes, abspath) -> (imports,
#: import_refs, import_bindings)``. Each language supplies one; the builder never
#: parses imports itself. ``root_node`` is the tree-sitter root; slicing uses the
#: bytes; ``abspath`` lets a language resolve its *relative* specifiers (JS
#: ``./util``, C ``#include "x.h"``) to the absolute path stem they target, so the
#: language-neutral within-set / dangling matcher can string-compare them against
#: a file's :meth:`ModuleResolver.module_candidates` the same way Python's
#: already-absolute dotted names are matched.
ImportExtractor = Callable[[Any, bytes, str], "tuple[list[str], list[ImportRef], list[ImportBinding]]"]

#: A name extractor for the awkward cases (C's nested declarators): given a def
#: node, return its bare name or ``None``. Most languages need none of this and
#: rely on ``name_field`` / ``name_node_types`` instead.
NameExtractor = Callable[[Any], Optional[str]]


@dataclass(frozen=True)
class DocRule:
    """How a language spells doc comments, for a best-effort one-line summary.

    ``comment_types`` are the tree-sitter node types that count as documentation
    (``comment``, ``line_comment``, ``block_comment``, …). Contiguous such nodes
    immediately preceding a definition (or leading the file, for the module
    summary) are read; their comment markers are stripped generically and the
    first meaningful line is kept.
    """

    comment_types: tuple[str, ...]


@dataclass(frozen=True)
class DefRule:
    """One rule mapping tree-sitter node types to a definition + optional scope.

    ``def_kind`` is the neutral :class:`~mote.runtime.code_map.scopes.Def` kind
    (``function`` / ``class`` / ``variable`` — only ``function``/``class`` are
    call-edge targets); ``symbol_kind`` is the free-form presentation label. When
    ``opens_scope`` the def introduces a ``scope_kind`` body scope the walker
    recurses into (a function/class/impl body). ``is_method_context`` upgrades the
    label to ``method`` when the def is bound directly in a class scope (for
    languages like C++ whose one ``function_definition`` node serves both).
    """

    node_types: tuple[str, ...]
    def_kind: str  # "function" | "class" | "variable"
    symbol_kind: str  # presentation label ("function"/"method"/"class"/"struct"/…)
    opens_scope: bool = False
    scope_kind: str = "function"  # ScopeKind opened when opens_scope ("function"/"class"/"block")
    name_field: str = "name"  # child_by_field_name to read the bare name
    name_node_types: tuple[str, ...] = ()  # fallback: first descendant of these types
    name_extractor: Optional[NameExtractor] = None  # last-resort custom name (C declarators)
    is_method_context: bool = False  # bound in a class scope → label becomes "method"
    params_field: str = ""  # child field sliced verbatim as the signature ("" → no sig)
    return_field: str = ""  # child field holding the return type, normalized to " -> ret"
    emit_symbol: bool = True  # variables bind a Def (for shadowing) but emit no Symbol


@dataclass(frozen=True)
class FieldAccess:
    """The ``recv.member`` node shape — how to read a method-call receiver + name.

    ``node_types`` are the member-access node types (``member_expression``,
    ``field_expression``, ``selector_expression``, ``field_access`` …);
    ``object_field`` / ``member_field`` are the child fields holding the receiver
    and the accessed name. Used only to decide whether ``recv.m()`` is a
    ``self``/``this`` call (a real same-file edge) or a foreign one (no edge).
    """

    node_types: tuple[str, ...]
    object_field: str
    member_field: str


@dataclass(frozen=True)
class LangConfig:
    """Everything the generic tree-sitter builder needs for one language."""

    language: str  # stable id ("javascript", "go", …)
    ts_name: str  # tree-sitter grammar name (get_parser(ts_name))
    extensions: tuple[str, ...]  # claimed file extensions (each incl. the dot)
    def_rules: tuple[DefRule, ...]
    call_node_types: tuple[str, ...]  # the call-expression node types
    call_func_field: str  # child field on a call node holding the callee expression
    identifier_node: str = "identifier"  # the bare-identifier node type
    field_access: Optional[FieldAccess] = None  # recv.member shape (self-call detection)
    self_receivers: frozenset[str] = frozenset()  # {"self","cls"} / {"this"} / {"self"}
    block_node_types: tuple[str, ...] = ()  # lexical-block node types (non-owning scopes)
    skip_class_scope: bool = False  # True only for Python-style class-scope skip
    doc_comment: Optional[DocRule] = None
    import_extractor: Optional[ImportExtractor] = None
    module_resolver_factory: Optional[Callable[[], ModuleResolver]] = None


__all__ = [
    "DocRule",
    "DefRule",
    "FieldAccess",
    "LangConfig",
    "ImportExtractor",
    "NameExtractor",
]
