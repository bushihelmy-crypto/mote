"""C / C++ language configs — declarative data + the ``#include`` reader.

Two configs share one file because C and C++ are one grammar family with the
same call/include shapes: :data:`C` (``.c`` / ``.h``) is the lean base —
``function_definition`` is the only def, a call is a bare-identifier
``call_expression`` — and :data:`CPP` (``.cpp`` / ``.cc`` / ``.cxx`` / ``.hpp`` /
``.hh``) layers on class-likes (``class_specifier`` / ``struct_specifier`` open
class scopes so a method resolves a sibling method) and ``namespace_definition``
(a lexical ``block`` scope so ``ns::`` names qualify and still resolve in-file).
Both draw a same-file method edge for a ``this->m()`` call (``field_expression``
receiver ``this``) and drop a foreign ``obj->m()`` — the standard shape.

The one awkward piece is C's name: a function's identifier is buried under nested
declarators (``int *make(void)`` → ``pointer_declarator`` → ``function_declarator``
→ ``identifier``), so :func:`_c_declarator_name` walks the ``declarator`` field
chain to the name leaf. :func:`c_includes` records each quoted ``#include "x.h"``
as the absolute path it resolves to (system ``<…>`` includes are dropped); the
:class:`CIncludeResolver` confirms that path on disk.

TRADE (documented): a C++ *out-of-line* definition (``void Widget::render() {…}``)
carries a ``qualified_identifier`` name (``Widget::render``) and lands in the file
module scope — a bare in-file ``render()`` call will not match it (matching a
qualified out-of-line body to its class needs the type resolution we deliberately
avoid). Inline methods and free functions resolve fully; the intra-file bare-call
and ``this->`` graph is complete.
"""

from __future__ import annotations

from typing import Any, Optional

from mote.context.code_map._langconfigs._shared import iter_nodes, resolve_relative_stem, string_text
from mote.context.code_map.model import ImportBinding, ImportRef
from mote.context.code_map.providers.config import DefRule, DocRule, FieldAccess, LangConfig
from mote.context.code_map.providers.resolvers.cfamily import CIncludeResolver

#: Declarator leaf types that carry a definition's bare (or qualified) name.
_NAME_LEAF_TYPES = frozenset(
    {"identifier", "field_identifier", "qualified_identifier", "destructor_name", "operator_name"}
)


def _c_declarator_name(node: Any) -> Optional[str]:
    """The name a ``function_definition`` introduces, from its nested declarators.

    A C/C++ function's name sits at the bottom of a ``declarator`` field chain
    (``pointer_declarator`` → ``function_declarator`` → ``identifier``; a C++
    method → ``field_identifier``; an out-of-line def → ``qualified_identifier``
    like ``Widget::render``). Walk the ``declarator`` field until a name leaf, and
    return its text (or ``None`` for an unnamed / unparsable declarator).
    """
    # The declarator chain is finite and strictly descending; a small depth bound
    # guards a pathological grammar (a fresh tree-sitter wrapper is returned each
    # hop, so id()-based cycle detection is unsafe — freed ids get reused).
    current = node
    for _ in range(16):
        if current is None:
            break
        if current.type in _NAME_LEAF_TYPES:
            return current.text.decode("utf-8", "replace")
        current = current.child_by_field_name("declarator")
    return None


def c_includes(root: Any, source: bytes, abspath: str) -> "tuple[list[str], list[ImportRef], list[ImportBinding]]":
    """Record quoted ``#include "x.h"`` as the absolute path it targets.

    A quoted include resolves relative to the including file, so it maps to a
    concrete absolute path (extension kept — a header names a real file); an
    angle-bracket ``#include <stdio.h>`` names a system header found only via the
    compiler's ``-I`` search and is dropped. Includes bind no local name (the
    preprocessor pastes the whole header) → module keys only, no refs/bindings.
    """
    imports: list[str] = []
    seen: set[str] = set()

    for node in iter_nodes(root):
        if node.type != "preproc_include":
            continue
        path_node = node.child_by_field_name("path")
        if path_node is None or path_node.type != "string_literal":
            continue  # system_lib_string (<…>) or malformed — dropped
        spec = string_text(path_node)
        if not spec:
            continue
        module = resolve_relative_stem(abspath, spec)  # absolute path, extension kept
        if module not in seen:
            seen.add(module)
            imports.append(module)

    return imports, [], []


C = LangConfig(
    language="c",
    ts_name="c",
    extensions=(".c", ".h"),
    def_rules=(
        DefRule(
            node_types=("function_definition",),
            def_kind="function",
            symbol_kind="function",
            opens_scope=True,
            scope_kind="function",
            is_method_context=True,
            name_extractor=_c_declarator_name,
            name_field="declarator",  # skip the declarator subtree when walking the body
        ),
    ),
    call_node_types=("call_expression",),
    call_func_field="function",
    identifier_node="identifier",
    field_access=FieldAccess(node_types=("field_expression",), object_field="argument", member_field="field"),
    self_receivers=frozenset(),  # C has no receiver-based method calls
    block_node_types=("compound_statement",),
    skip_class_scope=False,
    doc_comment=DocRule(comment_types=("comment",)),
    import_extractor=c_includes,
    module_resolver_factory=CIncludeResolver,
)


CPP = LangConfig(
    language="cpp",
    ts_name="cpp",
    extensions=(".cpp", ".cc", ".cxx", ".hpp", ".hh"),
    def_rules=(
        DefRule(
            node_types=("class_specifier",),
            def_kind="class",
            symbol_kind="class",
            opens_scope=True,
            scope_kind="class",
        ),
        DefRule(
            node_types=("struct_specifier",),
            def_kind="class",
            symbol_kind="struct",
            opens_scope=True,
            scope_kind="class",
        ),
        DefRule(
            node_types=("namespace_definition",),
            def_kind="class",  # not callable; def_kind is only used for edge targets
            symbol_kind="namespace",
            opens_scope=True,
            scope_kind="block",  # a lexical group: names qualify + resolve, owns no calls
        ),
        DefRule(
            node_types=("function_definition",),
            def_kind="function",
            symbol_kind="function",
            opens_scope=True,
            scope_kind="function",
            is_method_context=True,
            name_extractor=_c_declarator_name,
            name_field="declarator",
        ),
    ),
    call_node_types=("call_expression",),
    call_func_field="function",
    identifier_node="identifier",
    field_access=FieldAccess(node_types=("field_expression",), object_field="argument", member_field="field"),
    self_receivers=frozenset({"this"}),
    block_node_types=("compound_statement",),
    skip_class_scope=False,
    doc_comment=DocRule(comment_types=("comment",)),
    import_extractor=c_includes,
    module_resolver_factory=CIncludeResolver,
)


__all__ = ["C", "CPP", "c_includes"]
