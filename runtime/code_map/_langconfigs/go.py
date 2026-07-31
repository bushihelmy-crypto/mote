"""Go language config — declarative data + the import-spec reader.

The :data:`GO` :class:`LangConfig` maps Go's node types to the neutral model:
``function_declaration`` and ``method_declaration`` are call-graph roots (a method
is always a method in Go — labeled unconditionally, since Go has no class scope
wrapping it), ``type_spec`` is a type/struct symbol, and a ``selector_expression``
(``x.M()``) is a foreign receiver (Go has no ``self``/``this`` token) so it draws
no same-file edge — only bare-name ``Foo()`` calls resolve. :func:`go_imports`
records each ``import_spec``'s path verbatim (the :class:`GoModuleResolver` does the
``go.mod``-anchored path→directory mapping, so the raw import path is the key the
facade matches on).
"""

from __future__ import annotations

from typing import Any

from mote.runtime.code_map._langconfigs._shared import iter_nodes, string_text
from mote.runtime.code_map.model import ImportBinding, ImportRef
from mote.runtime.code_map.providers.config import DefRule, DocRule, FieldAccess, LangConfig
from mote.runtime.code_map.providers.resolvers.go import GoModuleResolver


def _line(node: Any) -> int:
    return node.start_point.row + 1


def _col(node: Any) -> int:
    return node.start_point.column


def go_imports(root: Any, source: bytes, abspath: str) -> "tuple[list[str], list[ImportRef], list[ImportBinding]]":
    """Extract Go imports from ``import_spec`` nodes (single or grouped).

    Each ``import_spec`` carries a ``path`` (the module path string) and an
    optional ``name`` alias (``m "x/y"``, ``_ "x/y"``, ``. "x/y"``). The bound
    local name is the alias when present, else the path's last segment; the
    module key is the raw import path (the resolver maps it to a directory).
    """
    imports: list[str] = []
    seen: set[str] = set()
    refs: list[ImportRef] = []
    bindings: list[ImportBinding] = []

    for node in iter_nodes(root):
        if node.type != "import_spec":
            continue
        path_node = node.child_by_field_name("path")
        if path_node is None:
            continue
        module = string_text(path_node)
        if not module:
            continue
        if module not in seen:
            seen.add(module)
            imports.append(module)
        name_node = node.child_by_field_name("name")
        if name_node is not None:
            local = name_node.text.decode("utf-8", "replace")
            pos = name_node
        else:
            local = module.rsplit("/", 1)[-1]
            pos = path_node
        # A ``_`` (blank) or ``.`` (dot) import binds no usable local name.
        if local in ("_", "."):
            continue
        refs.append(
            ImportRef(
                module=module,
                name=local,
                line=_line(pos),
                col=_col(pos),
                imported_name="",
            )
        )
        bindings.append(ImportBinding(local_name=local, module=module, imported_name="", line=_line(pos)))

    return imports, refs, bindings


GO = LangConfig(
    language="go",
    ts_name="go",
    extensions=(".go",),
    def_rules=(
        DefRule(
            node_types=("function_declaration",),
            def_kind="function",
            symbol_kind="function",
            opens_scope=True,
            scope_kind="function",
            params_field="parameters",
            return_field="result",
        ),
        DefRule(
            node_types=("method_declaration",),
            def_kind="function",
            symbol_kind="method",
            opens_scope=True,
            scope_kind="function",
            name_field="name",
            params_field="parameters",
            return_field="result",
        ),
        DefRule(
            node_types=("type_spec",),
            def_kind="class",
            symbol_kind="type",
        ),
    ),
    call_node_types=("call_expression",),
    call_func_field="function",
    identifier_node="identifier",
    field_access=FieldAccess(
        node_types=("selector_expression",),
        object_field="operand",
        member_field="field",
    ),
    self_receivers=frozenset(),  # Go has no self/this receiver token
    block_node_types=("block",),
    skip_class_scope=False,
    doc_comment=DocRule(comment_types=("comment",)),
    import_extractor=go_imports,
    module_resolver_factory=GoModuleResolver,
)


__all__ = ["GO", "go_imports"]
