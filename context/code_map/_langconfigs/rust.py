"""Rust language config — declarative data + the ``use`` / ``mod`` reader.

The :data:`RUST` :class:`LangConfig` maps Rust's node types to the neutral model:
``function_item`` and ``function_signature_item`` are call-graph roots (labeled
``method`` when bound in a class-like scope), ``struct_item`` / ``enum_item`` /
``trait_item`` are type symbols, and ``impl_item`` opens a ``"class"`` scope so a
``self.m()`` call inside an ``impl`` resolves to the sibling method it defines
(``self`` is the receiver token). ``mod_item`` is a module symbol *and* an import:
:func:`rust_use` records each ``use`` path (crate-anchored ``crate::a::b`` or
external ``std::…``) and each ``mod name;`` declaration (pre-resolved to the
sibling file's absolute path stem), and the :class:`RustModuleResolver` does the
``Cargo.toml``-anchored path→file mapping.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from mote.context.code_map._langconfigs._shared import iter_nodes
from mote.context.code_map.model import ImportBinding, ImportRef
from mote.context.code_map.providers.config import DefRule, DocRule, FieldAccess, LangConfig
from mote.context.code_map.providers.resolvers.rust import RustModuleResolver


def _line(node: Any) -> int:
    return node.start_point.row + 1


def _col(node: Any) -> int:
    return node.start_point.column


def _path_str(node: Any) -> str:
    """A path node's ``::``-joined text (``crate::a::b``), whitespace-stripped."""
    return node.text.decode("utf-8", "replace").strip()


def _join(prefix: str, tail: str) -> str:
    """Join a ``scoped_use_list`` base prefix with a leaf item into one path."""
    return f"{prefix}::{tail}" if prefix else tail


def _last_segment(path: str) -> str:
    """The bound local name of a ``use`` path — its final ``::`` segment."""
    return path.rsplit("::", 1)[-1]


def _walk_use(arg: Any, prefix: str, out: "list[tuple[str, Optional[str]]]") -> None:
    """Flatten a ``use`` tree into ``(module_path, local_name|None)`` pairs.

    Handles the nested shapes: a plain path (``crate::a::Item``), an ``X as Y``
    alias, a ``{A, B as C}`` grouped list (recursing under the shared prefix), and
    a ``*`` glob (a path but no bound name). A glob or a nested list contributes no
    single local binding for the group node itself.
    """
    t = arg.type
    if t == "scoped_use_list":
        path_node = arg.child_by_field_name("path")
        base = _join(prefix, _path_str(path_node)) if path_node is not None else prefix
        lst = arg.child_by_field_name("list")
        if lst is not None:
            for item in lst.named_children:
                _walk_use(item, base, out)
        return
    if t == "use_list":
        for item in arg.named_children:
            _walk_use(item, prefix, out)
        return
    if t == "use_as_clause":
        path_node = arg.child_by_field_name("path")
        alias_node = arg.child_by_field_name("alias")
        if path_node is None:
            return
        full = _join(prefix, _path_str(path_node))
        local = _path_str(alias_node) if alias_node is not None else _last_segment(full)
        out.append((full, local))
        return
    if t == "use_wildcard":
        base = arg.named_children[0] if arg.named_children else None
        full = _join(prefix, _path_str(base)) if base is not None else prefix
        out.append((full, None))
        return
    # A plain path (scoped_identifier / identifier / crate / self / super / …).
    full = _join(prefix, _path_str(arg))
    out.append((full, _last_segment(full)))


def rust_use(root: Any, source: bytes, abspath: str) -> "tuple[list[str], list[ImportRef], list[ImportBinding]]":
    """Extract Rust ``use`` paths and ``mod`` declarations.

    Each ``use`` path becomes a module key (``crate::a::b`` resolves against the
    crate ``src`` root; ``std::…`` is external → dropped by the resolver). Each
    ``mod name;`` is pre-resolved to the sibling module file's absolute path stem
    (``<dir>/name``) so the resolver's absolute-stem probe finds ``name.rs`` |
    ``name/mod.rs`` — the language-neutral matcher then string-compares it.
    """
    imports: list[str] = []
    seen: set[str] = set()
    refs: list[ImportRef] = []
    bindings: list[ImportBinding] = []
    directory = os.path.dirname(abspath)

    def record(module: str, local: Optional[str], node: Any) -> None:
        if not module:
            return
        if module not in seen:
            seen.add(module)
            imports.append(module)
        if local in (None, "_"):
            return
        refs.append(ImportRef(module=module, name=local, line=_line(node), col=_col(node), imported_name=""))
        bindings.append(ImportBinding(local_name=local, module=module, imported_name="", line=_line(node)))

    for node in iter_nodes(root):
        if node.type == "use_declaration":
            arg = node.named_children[0] if node.named_children else None
            if arg is None:
                continue
            pairs: list[tuple[str, Optional[str]]] = []
            _walk_use(arg, "", pairs)
            for module, local in pairs:
                record(module, local, node)
        elif node.type == "mod_item":
            # ``mod name;`` (no body) declares a sibling module file; ``mod name {…}``
            # (inline body) declares no file. Only the declaration form is an import.
            if node.child_by_field_name("body") is not None:
                continue
            name_node = node.child_by_field_name("name")
            if name_node is None:
                continue
            name = name_node.text.decode("utf-8", "replace")
            stem = os.path.join(directory, name)
            record(stem, name, name_node)

    return imports, refs, bindings


RUST = LangConfig(
    language="rust",
    ts_name="rust",
    extensions=(".rs",),
    def_rules=(
        DefRule(
            node_types=("function_item",),
            def_kind="function",
            symbol_kind="function",
            opens_scope=True,
            scope_kind="function",
            is_method_context=True,  # inside an impl/trait scope → labeled "method"
            params_field="parameters",
            return_field="return_type",
        ),
        DefRule(
            node_types=("function_signature_item",),
            def_kind="function",
            symbol_kind="method",  # a trait method signature — symbol, but no body/scope
            is_method_context=True,
            params_field="parameters",
            return_field="return_type",
        ),
        # ``impl Point { … }`` opens a class-like scope (named by its ``type`` field,
        # not ``name``) so ``self.m()`` inside resolves to a sibling method; the impl
        # itself is structural, not a presented symbol.
        DefRule(
            node_types=("impl_item",),
            def_kind="class",
            symbol_kind="impl",
            opens_scope=True,
            scope_kind="class",
            name_field="type",
            emit_symbol=False,
        ),
        DefRule(
            node_types=("trait_item",),
            def_kind="class",
            symbol_kind="trait",
            opens_scope=True,
            scope_kind="class",
        ),
        DefRule(node_types=("struct_item",), def_kind="class", symbol_kind="struct"),
        DefRule(node_types=("enum_item",), def_kind="class", symbol_kind="enum"),
        DefRule(node_types=("mod_item",), def_kind="class", symbol_kind="module"),
    ),
    call_node_types=("call_expression",),
    call_func_field="function",
    identifier_node="identifier",
    field_access=FieldAccess(node_types=("field_expression",), object_field="value", member_field="field"),
    self_receivers=frozenset({"self"}),
    block_node_types=("block",),
    skip_class_scope=False,
    doc_comment=DocRule(comment_types=("line_comment", "block_comment")),
    import_extractor=rust_use,
    module_resolver_factory=RustModuleResolver,
)


__all__ = ["RUST", "rust_use"]
