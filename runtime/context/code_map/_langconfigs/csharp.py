"""C# language config — declarative data + the ``using`` reader.

The :data:`CSHARP` :class:`LangConfig` maps C#'s node types to the neutral model:
``class`` / ``interface`` / ``struct`` / ``record_declaration`` open class-like
scopes, ``method_declaration`` / ``constructor_declaration`` are the call-graph
roots (labeled ``method``), and ``variable_declarator`` binds a (symbol-less)
variable for shadowing. A call is an ``invocation_expression`` whose ``function``
is either a bare ``identifier`` (a same-file call) or a ``member_access_expression``
(``this.M()`` draws a same-file edge via the ``this`` receiver; a foreign
``obj.M()`` draws none) — the standard shape the generic builder already handles.

TRADE (documented): a ``namespace`` is *not* modeled as a scope and there is **no
module resolver** — C# namespaces are logical, with no required correspondence to
the filesystem (a type ``App.Core.Service`` need not live at ``App/Core/Service.cs``),
so a file-anchored ``module→file`` map would be a guess, and a wrong cross-file
edge is worse than a missing one. Types therefore land in the file's module scope
(cross-type calls within a file still resolve) and ``using`` directives are recorded
only informationally (no cross-file import edges). The intra-file call graph is full.
"""

from __future__ import annotations

from typing import Any

from mote.runtime.context.code_map._langconfigs._shared import iter_nodes
from mote.runtime.context.code_map.model import ImportBinding, ImportRef
from mote.runtime.context.code_map.providers.config import DefRule, DocRule, FieldAccess, LangConfig


def _line(node: Any) -> int:
    return node.start_point.row + 1


def _col(node: Any) -> int:
    return node.start_point.column


def csharp_usings(root: Any, source: bytes, abspath: str) -> "tuple[list[str], list[ImportRef], list[ImportBinding]]":
    """Record ``using`` namespaces as informational module keys (no bindings).

    ``using System.Collections.Generic;`` opens a namespace — it binds no single
    local name (a namespace makes *all* its names visible), so it yields a module
    key but no :class:`ImportRef` / :class:`ImportBinding`. With no C# module
    resolver these keys never resolve to a file (the documented trade); they are
    kept so the extract still reflects the file's declared dependencies.
    """
    imports: list[str] = []
    seen: set[str] = set()

    for node in iter_nodes(root):
        if node.type != "using_directive":
            continue
        # The namespace path is the directive's qualified_name / identifier child;
        # an alias/static using still carries it — take the last such child.
        path_node = None
        for child in node.named_children:
            if child.type in ("qualified_name", "identifier"):
                path_node = child
        if path_node is None:
            continue
        module = path_node.text.decode("utf-8", "replace").strip()
        if module and module not in seen:
            seen.add(module)
            imports.append(module)

    return imports, [], []


CSHARP = LangConfig(
    language="csharp",
    ts_name="csharp",
    extensions=(".cs",),
    def_rules=(
        DefRule(
            node_types=("class_declaration",),
            def_kind="class",
            symbol_kind="class",
            opens_scope=True,
            scope_kind="class",
        ),
        DefRule(
            node_types=("interface_declaration",),
            def_kind="class",
            symbol_kind="interface",
            opens_scope=True,
            scope_kind="class",
        ),
        DefRule(
            node_types=("struct_declaration",),
            def_kind="class",
            symbol_kind="struct",
            opens_scope=True,
            scope_kind="class",
        ),
        DefRule(
            node_types=("record_declaration",),
            def_kind="class",
            symbol_kind="record",
            opens_scope=True,
            scope_kind="class",
        ),
        DefRule(
            node_types=("method_declaration",),
            def_kind="function",
            symbol_kind="method",
            opens_scope=True,
            scope_kind="function",
            is_method_context=True,
            params_field="parameters",
            return_field="returns",
        ),
        DefRule(
            node_types=("constructor_declaration",),
            def_kind="function",
            symbol_kind="method",
            opens_scope=True,
            scope_kind="function",
            is_method_context=True,
            params_field="parameters",
        ),
        DefRule(
            node_types=("variable_declarator",),
            def_kind="variable",
            symbol_kind="variable",
            emit_symbol=False,
        ),
    ),
    call_node_types=("invocation_expression",),
    call_func_field="function",
    identifier_node="identifier",
    field_access=FieldAccess(node_types=("member_access_expression",), object_field="expression", member_field="name"),
    self_receivers=frozenset({"this"}),
    block_node_types=("block",),
    skip_class_scope=False,
    doc_comment=DocRule(comment_types=("comment",)),
    import_extractor=csharp_usings,
    module_resolver_factory=None,  # documented trade — C# has no filesystem-derivable module map
)


__all__ = ["CSHARP", "csharp_usings"]
