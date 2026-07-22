"""JavaScript language config — the declarative data + the ES-module import reader.

The :data:`JAVASCRIPT` :class:`LangConfig` is pure data the generic tree-sitter
builder reads to emit the neutral scope graph + symbols (which node types are
functions/classes/methods/variables, how a ``recv.m()`` call reads its receiver,
how doc comments are spelled). The one piece of *code* here is :func:`js_esm` —
the import extractor — because JS import resolution is path-based and needs the
importing file's own path to turn a relative specifier (``./util``) into the
absolute path *stem* it targets, which the language-neutral matcher then string-
compares against a file's :meth:`JsModuleResolver.module_candidates` exactly as it
does Python's dotted names.

``skip_class_scope=False``: unlike Python, a JS method resolves a sibling method
by the plain lexical walk (``this.m()`` sets ``via_self`` so it also resolves), so
the class scope is a real intermediate scope, not skipped.
"""

from __future__ import annotations

from typing import Any

from mote.context.code_map._langconfigs._shared import is_relative_spec, iter_nodes, resolve_relative_stem, string_text
from mote.context.code_map.model import ImportBinding, ImportRef
from mote.context.code_map.providers.config import DefRule, DocRule, FieldAccess, LangConfig
from mote.context.code_map.providers.resolvers.javascript import JsModuleResolver

#: ES-module default import binds the module's default export; we record it under
#: this canonical imported-name so a reverse-dep query can match "who imports the
#: default of X" distinctly from a named export.
_DEFAULT_EXPORT = "default"


def _line(node: Any) -> int:
    return node.start_point.row + 1


def _col(node: Any) -> int:
    return node.start_point.column


def _resolve_spec(spec: str, abspath: str) -> str:
    """A specifier → the module key the facade matches on.

    Relative (``./util``, ``../lib/x``) → the absolute path stem it targets (so it
    string-matches a repo file's candidate); a bare package (``lodash``) is kept
    verbatim — it names a ``node_modules`` dependency the resolver deliberately
    maps to nothing (external).
    """
    if is_relative_spec(spec):
        return resolve_relative_stem(abspath, spec)
    return spec


def _string_child(node: Any) -> Any:
    """The first ``string`` node among *node*'s named children, or ``None``."""
    for child in node.named_children:
        if child.type == "string":
            return child
    return None


def js_esm(root: Any, source: bytes, abspath: str) -> "tuple[list[str], list[ImportRef], list[ImportBinding]]":
    """Extract ES-module + CommonJS imports from a JS tree.

    Handles ``import {a, b as c} from "./m"`` (named), ``import d from "./m"``
    (default), ``import * as ns from "./m"`` (namespace), and ``const x =
    require("./m")`` (CommonJS). Each yields the resolved module key plus a
    per-binding :class:`ImportRef` and :class:`ImportBinding` — the same neutral
    rows the Python provider emits — so cross-file edge drawing is language-neutral.
    """
    imports: list[str] = []
    seen: set[str] = set()
    refs: list[ImportRef] = []
    bindings: list[ImportBinding] = []

    def record_module(module: str) -> None:
        if module and module not in seen:
            seen.add(module)
            imports.append(module)

    def bind(module: str, local: str, imported: str, name_node: Any) -> None:
        refs.append(
            ImportRef(
                module=module,
                name=local,
                line=_line(name_node),
                col=_col(name_node),
                imported_name=imported,
            )
        )
        bindings.append(ImportBinding(local_name=local, module=module, imported_name=imported, line=_line(name_node)))

    for node in iter_nodes(root):
        if node.type == "import_statement":
            source_node = node.child_by_field_name("source") or _string_child(node)
            if source_node is None:
                continue
            module = _resolve_spec(string_text(source_node), abspath)
            record_module(module)
            clause = None
            for child in node.named_children:
                if child.type == "import_clause":
                    clause = child
                    break
            if clause is None:
                continue  # bare side-effect import (``import "./m"``) — module only
            for member in clause.named_children:
                if member.type == "identifier":
                    # ``import def from "./m"`` — default export binding.
                    bind(module, string_text(member), _DEFAULT_EXPORT, member)
                elif member.type == "namespace_import":
                    # ``import * as ns from "./m"`` — whole-module binding.
                    ident = member.named_children[0] if member.named_children else None
                    if ident is not None:
                        bind(module, string_text(ident), "", ident)
                elif member.type == "named_imports":
                    for spec in member.named_children:
                        if spec.type != "import_specifier":
                            continue
                        idents = [c for c in spec.named_children if c.type == "identifier"]
                        if not idents:
                            continue
                        imported = string_text(idents[0])
                        local = string_text(idents[1]) if len(idents) > 1 else imported
                        bind(module, local, imported, idents[-1])
        elif node.type == "call_expression":
            func = node.child_by_field_name("function")
            if func is None or func.type != "identifier" or func.text != b"require":
                continue
            args = node.child_by_field_name("arguments")
            arg_str = _string_child(args) if args is not None else None
            if arg_str is None:
                continue
            module = _resolve_spec(string_text(arg_str), abspath)
            record_module(module)
            # ``const x = require("./m")`` — bind x to the whole module when the
            # require sits directly in a simple variable declarator.
            parent = node.parent
            if parent is not None and parent.type == "variable_declarator":
                name_node = parent.child_by_field_name("name")
                if name_node is not None and name_node.type == "identifier":
                    bind(module, string_text(name_node), "", name_node)

    return imports, refs, bindings


JAVASCRIPT = LangConfig(
    language="javascript",
    ts_name="javascript",
    extensions=(".js", ".jsx", ".mjs", ".cjs"),
    def_rules=(
        DefRule(
            node_types=("function_declaration", "generator_function_declaration"),
            def_kind="function",
            symbol_kind="function",
            opens_scope=True,
            scope_kind="function",
            params_field="parameters",
        ),
        DefRule(
            node_types=("class_declaration",),
            def_kind="class",
            symbol_kind="class",
            opens_scope=True,
            scope_kind="class",
        ),
        DefRule(
            node_types=("method_definition",),
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
    call_node_types=("call_expression",),
    call_func_field="function",
    identifier_node="identifier",
    field_access=FieldAccess(node_types=("member_expression",), object_field="object", member_field="property"),
    self_receivers=frozenset({"this"}),
    block_node_types=("statement_block",),
    skip_class_scope=False,
    doc_comment=DocRule(comment_types=("comment",)),
    import_extractor=js_esm,
    module_resolver_factory=JsModuleResolver,
)


__all__ = ["JAVASCRIPT", "js_esm"]
