"""Java language config — declarative data + the import reader.

The :data:`JAVA` :class:`LangConfig` maps Java's node types to the neutral model:
``class`` / ``interface`` / ``enum_declaration`` open class-like scopes,
``method_declaration`` / ``constructor_declaration`` are the call-graph roots
(labeled ``method`` since they always sit in a class scope), and
``variable_declarator`` binds a (symbol-less) variable for shadowing. A call is a
``method_invocation`` — Java's invocation node carries the receiver (``object``)
and method (``name``) on itself, which the generic builder reads via the
:class:`FieldAccess` mapping to draw a ``this.m()`` same-file edge (``this`` is the
receiver token) while dropping a foreign ``obj.m()``. :func:`java_imports` records
each ``import_declaration``'s fully-qualified type name; the
:class:`JavaModuleResolver` does the ``package``-anchored ``a.b.C`` → ``a/b/C.java``
mapping.
"""

from __future__ import annotations

from typing import Any

from mote.context.code_map._langconfigs._shared import iter_nodes
from mote.context.code_map.model import ImportBinding, ImportRef
from mote.context.code_map.providers.config import DefRule, DocRule, FieldAccess, LangConfig
from mote.context.code_map.providers.resolvers.java import JavaModuleResolver


def _line(node: Any) -> int:
    return node.start_point.row + 1


def _col(node: Any) -> int:
    return node.start_point.column


def java_imports(root: Any, source: bytes, abspath: str) -> "tuple[list[str], list[ImportRef], list[ImportBinding]]":
    """Extract Java imports from ``import_declaration`` nodes.

    An import names a fully-qualified type (``com.example.util.Helper``) whose last
    segment is the bound local name; a ``static`` import (``…Const.MAX``) names a
    member — kept whole as the module key (the resolver probes the name minus its
    last segment to reach ``Const.java``). A wildcard (``com.example.*``) binds no
    single name but its package is still recorded as a module key.
    """
    imports: list[str] = []
    seen: set[str] = set()
    refs: list[ImportRef] = []
    bindings: list[ImportBinding] = []

    for node in iter_nodes(root):
        if node.type != "import_declaration":
            continue
        name_node = node.child_by_field_name("name")
        # A scoped/plain name child holds the dotted path (field name absent on
        # some grammar versions → fall back to the first scoped_identifier child).
        if name_node is None:
            for child in node.named_children:
                if child.type in ("scoped_identifier", "identifier"):
                    name_node = child
                    break
        if name_node is None:
            continue
        module = name_node.text.decode("utf-8", "replace").strip()
        if not module:
            continue
        is_wildcard = any(c.type == "asterisk" for c in node.named_children) or module.endswith(".")
        if module not in seen:
            seen.add(module)
            imports.append(module)
        if is_wildcard:
            continue
        local = module.rsplit(".", 1)[-1]
        refs.append(ImportRef(module=module, name=local, line=_line(node), col=_col(node), imported_name=""))
        bindings.append(ImportBinding(local_name=local, module=module, imported_name="", line=_line(node)))

    return imports, refs, bindings


JAVA = LangConfig(
    language="java",
    ts_name="java",
    extensions=(".java",),
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
            node_types=("enum_declaration",),
            def_kind="class",
            symbol_kind="enum",
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
            return_field="type",
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
    call_node_types=("method_invocation",),
    call_func_field="name",  # unused for Java (the invocation node is the field-access carrier)
    identifier_node="identifier",
    field_access=FieldAccess(node_types=("method_invocation",), object_field="object", member_field="name"),
    self_receivers=frozenset({"this"}),
    block_node_types=("block",),
    skip_class_scope=False,
    doc_comment=DocRule(comment_types=("block_comment", "line_comment")),
    import_extractor=java_imports,
    module_resolver_factory=JavaModuleResolver,
)


__all__ = ["JAVA", "java_imports"]
