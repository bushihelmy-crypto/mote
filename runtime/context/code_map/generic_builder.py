"""TreeSitterBuilder — the ONE config-driven walk from a tree-sitter tree to the
neutral :class:`~mote.runtime.context.code_map.scopes.ScopeGraph` + presentation symbols.

This is the tree-sitter twin of the Python ``ast`` walk
(:class:`~mote.runtime.context.code_map.providers.python._ScopeGraphBuilder`): given a
:class:`~mote.runtime.context.code_map.providers.config.LangConfig` (pure data — which
node types introduce defs / scopes / calls, the receiver tokens that make
``recv.m()`` a same-file method call, how doc comments are spelled) it emits the
*same* neutral rows every provider does. So the shared resolver
(:meth:`ScopeGraph.call_edges`) and the shared call-edge post-step in the facade
work for every language with **zero** per-language resolution code — the full
intra-file call graph is free.

The walk mirrors the Python builder's shape exactly: a def node opens a body
scope (recursed under a ``qualified.`` prefix), a lexical-block node opens a
non-owning ``"block"`` scope, a call node reads its callee field (bare identifier
→ a real call ref; ``self``/``this`` receiver → a ``via_self`` method ref; a
foreign receiver → no edge, only the receiver object is walked — identical to
Python's attribute handling), and a bare identifier is a load ref. Names,
signatures, and doc summaries are byte-sliced from the source via each node's
``text``. A fresh builder is constructed per file (it holds mutable walk state);
the provider never shares one across the threaded indexer.
"""

from __future__ import annotations

from typing import Any, Optional

from mote.runtime.context.code_map.model import SUMMARY_MAX_CHARS, Symbol
from mote.runtime.context.code_map.providers.config import DefRule, DocRule, LangConfig
from mote.runtime.context.code_map.scopes import Def, Ref, Scope, ScopeGraph

#: Comment markers stripped generically from a doc line, longest-first so
#: ``///`` beats ``//`` and ``/**`` beats ``/*``. Covers C-family (``//``,
#: ``/* */``, ``///`` doc, ``//!`` inner), scripting (``#``, ``#!``), and the
#: leading ``*`` of a javadoc continuation line.
_DOC_MARKERS = ("///", "//!", "//", "/**", "/*", "#!", "#", "*")


def _text(node: Any) -> str:
    """The source slice a node spans, decoded lenient (bytes → str)."""
    return node.text.decode("utf-8", "replace")


def _return_suffix(node: Any, rule: DefRule) -> str:
    """Normalize a rule's return-type field to a uniform `` -> ret`` suffix.

    Grammars spell the return type differently — TS/Rust hang it after the params
    (TS as ``: T``, Rust as ``-> T``), Go's ``result`` is a parenthesized tuple,
    Java/C# put a bare type *before* the name. We slice whichever field the rule
    names and re-render it in the one Python-style ``-> ret`` form the outline
    already uses, stripping any language-specific leading ``:`` / ``->`` token so
    every language reads alike. ``""`` when the rule names no field, the field is
    absent (a void/inferred return), or the slice is blank.
    """
    if not rule.return_field:
        return ""
    ret_node = node.child_by_field_name(rule.return_field)
    if ret_node is None:
        return ""
    ret = _text(ret_node).strip()
    for lead in ("->", ":"):
        if ret.startswith(lead):
            ret = ret[len(lead) :].strip()
            break
    return f" -> {ret}" if ret else ""


def _line(node: Any) -> int:
    """1-based start line of *node* (tree-sitter points are 0-based rows)."""
    return node.start_point.row + 1


def _col(node: Any) -> int:
    """0-based start column of *node* (LSP-style character offset)."""
    return node.start_point.column


def _clean_doc_line(text: str) -> str:
    """First meaningful line of a comment block, markers stripped + trimmed."""
    for raw in text.splitlines():
        line = raw.strip()
        # Strip leading comment markers repeatedly (e.g. ``/// `` then a stray ``*``).
        changed = True
        while changed:
            changed = False
            for marker in _DOC_MARKERS:
                if line.startswith(marker):
                    line = line[len(marker) :].strip()
                    changed = True
                    break
        if line.endswith("*/"):
            line = line[:-2].strip()
        if line:
            line = " ".join(line.split())  # collapse internal whitespace runs
            if len(line) > SUMMARY_MAX_CHARS:
                line = line[: SUMMARY_MAX_CHARS - 1].rstrip() + "…"
            return line
    return ""


def module_summary(root: Any, doc: Optional[DocRule]) -> str:
    """Best-effort one-line summary from a file's leading comment block."""
    if doc is None:
        return ""
    comments: list[Any] = []
    for child in root.children:
        if child.type in doc.comment_types:
            comments.append(child)
        else:
            break  # first non-comment node ends the leading block
    if not comments:
        return ""
    return _clean_doc_line("\n".join(_text(c) for c in comments))


class TreeSitterBuilder:
    """One config-driven walk building a :class:`ScopeGraph` + presentation symbols.

    Construct with a :class:`LangConfig`, call :meth:`build` with a tree-sitter
    root node. Stateful (accumulates scopes/defs/refs/symbols) so a fresh
    instance is made per file — never shared across threads.
    """

    def __init__(self, config: LangConfig) -> None:
        self._config = config
        # Precompute the per-node-type lookups the walk consults hot.
        self._rule_by_type: dict[str, DefRule] = {}
        for rule in config.def_rules:
            for ntype in rule.node_types:
                self._rule_by_type.setdefault(ntype, rule)
        self._block_types = frozenset(config.block_node_types)
        self._call_types = frozenset(config.call_node_types)

        self.scopes: dict[int, Scope] = {}
        self.defs: list[Def] = []
        self.refs: list[Ref] = []
        self.symbols: list[Symbol] = []
        self._next_id = 0

    def build(self, root: Any) -> tuple[ScopeGraph, list[Symbol]]:
        """Walk *root* into a resolved scope graph + the file's symbols."""
        module_scope = self._new_scope("module", None, 1)
        for child in root.named_children:
            self._walk(child, module_scope, prefix="")
        graph = ScopeGraph(
            scopes=self.scopes,
            defs=self.defs,
            refs=self.refs,
            skip_class_scope=self._config.skip_class_scope,
            self_receivers=self._config.self_receivers,
        )
        return graph, self.symbols

    # -- scope bookkeeping ---------------------------------------------------

    def _new_scope(self, kind: str, parent: Optional[int], line: int) -> int:
        sid = self._next_id
        self._next_id += 1
        self.scopes[sid] = Scope(id=sid, kind=kind, parent=parent, start_line=line)  # type: ignore[arg-type]
        return sid

    def _scope_kind(self, scope_id: int) -> str:
        scope = self.scopes.get(scope_id)
        return scope.kind if scope is not None else ""

    # -- the walk ------------------------------------------------------------

    def _walk(self, node: Any, scope_id: int, prefix: str) -> None:
        ntype = node.type
        rule = self._rule_by_type.get(ntype)
        if rule is not None:
            name = self._def_name(node, rule)
            if name is not None:
                self._handle_def(node, rule, name, scope_id, prefix)
                return
            # Unnamed def node (rare) — fall through to a generic recurse.
        if ntype in self._block_types:
            block_scope = self._new_scope("block", scope_id, _line(node))
            for child in node.named_children:
                self._walk(child, block_scope, prefix)
            return
        if ntype in self._call_types:
            self._handle_call(node, scope_id, prefix)
            return
        if ntype == self._config.identifier_node:
            self.refs.append(Ref(name=_text(node), scope=scope_id, line=_line(node), col=_col(node)))
            return
        for child in node.named_children:
            self._walk(child, scope_id, prefix)

    def _def_name(self, node: Any, rule: DefRule) -> Optional[str]:
        """The bare name a def node introduces, by the rule's naming strategy."""
        if rule.name_extractor is not None:
            return rule.name_extractor(node)
        if rule.name_field:
            child = node.child_by_field_name(rule.name_field)
            if child is not None:
                return _text(child)
        if rule.name_node_types:
            found = self._first_of_types(node, rule.name_node_types)
            if found is not None:
                return _text(found)
        return None

    def _handle_def(self, node: Any, rule: DefRule, name: str, scope_id: int, prefix: str) -> None:
        qualified = f"{prefix}{name}"
        kind = rule.symbol_kind
        if rule.is_method_context and self._scope_kind(scope_id) == "class":
            kind = "method"

        body_scope: Optional[int] = None
        if rule.opens_scope:
            body_scope = self._new_scope(rule.scope_kind, scope_id, _line(node))

        if rule.emit_symbol:
            signature = ""
            if rule.params_field:
                params_node = node.child_by_field_name(rule.params_field)
                if params_node is not None:
                    signature = _text(params_node)
                    signature += _return_suffix(node, rule)
            self.symbols.append(
                Symbol(
                    name=name,
                    qualified_name=qualified,
                    kind=kind,
                    start_line=_line(node),
                    signature=signature,
                    summary=self._doc_summary(node),
                )
            )

        self.defs.append(
            Def(
                name=name,
                scope=scope_id,
                line=_line(node),
                kind=rule.def_kind,  # type: ignore[arg-type]
                qualified_name=qualified,
                body_scope=body_scope,
            )
        )

        # Recurse the body under the introduced scope (+prefix) when one opened,
        # else stay in the enclosing scope. Skip the name node itself so the def's
        # own name is not re-emitted as a load ref.
        if body_scope is not None:
            child_scope, child_prefix = body_scope, f"{qualified}."
        else:
            child_scope, child_prefix = scope_id, prefix
        name_node = node.child_by_field_name(rule.name_field) if rule.name_field else None
        for child in node.named_children:
            if child is name_node:
                continue
            self._walk(child, child_scope, child_prefix)

    def _handle_call(self, node: Any, scope_id: int, prefix: str) -> None:
        fa = self._config.field_access
        # Java-style invocation: the call node itself carries the receiver
        # (``object``) + method (``name``) fields — there is no separate callee
        # sub-node. A missing object is a bare call; a ``self``/``this`` object is
        # a same-file method call; any other object is foreign (no edge, object
        # still walked for its inner refs).
        if fa is not None and node.type in fa.node_types:
            obj = node.child_by_field_name(fa.object_field)
            member = node.child_by_field_name(fa.member_field)
            obj_is_self = obj is not None and _text(obj) in self._config.self_receivers
            if member is not None and (obj is None or obj_is_self):
                self.refs.append(
                    Ref(
                        name=_text(member),
                        scope=scope_id,
                        line=_line(member),
                        col=_col(member),
                        is_call=True,
                        via_self=obj_is_self,
                    )
                )
            for child in node.named_children:
                if child is member:
                    continue  # the method name is not a load ref
                if child is obj and obj_is_self:
                    continue  # a self/this receiver token is not a ref
                self._walk(child, scope_id, prefix)
            return
        func = node.child_by_field_name(self._config.call_func_field)
        walked_func = False
        if func is not None:
            if func.type == self._config.identifier_node:
                self.refs.append(Ref(name=_text(func), scope=scope_id, line=_line(func), col=_col(func), is_call=True))
                walked_func = True
            else:
                fa = self._config.field_access
                if fa is not None and func.type in fa.node_types:
                    obj = func.child_by_field_name(fa.object_field)
                    member = func.child_by_field_name(fa.member_field)
                    if obj is not None and member is not None and _text(obj) in self._config.self_receivers:
                        # ``self.m()`` / ``this.m()`` — a real same-file method call.
                        self.refs.append(
                            Ref(
                                name=_text(member),
                                scope=scope_id,
                                line=_line(member),
                                col=_col(member),
                                is_call=True,
                                via_self=True,
                            )
                        )
                    elif obj is not None:
                        # Foreign receiver — no same-file edge; still walk the
                        # object part to capture its inner refs (mirrors Python's
                        # attribute handling, which never refs the member name).
                        self._walk(obj, scope_id, prefix)
                    walked_func = True
        # Recurse arguments; walk the callee generically only if we did not handle
        # it above (a complex callee expression, e.g. an indexed / parenthesized call).
        for child in node.named_children:
            if child is func:
                if not walked_func:
                    self._walk(child, scope_id, prefix)
                continue
            self._walk(child, scope_id, prefix)

    # -- doc + descendant helpers --------------------------------------------

    def _doc_summary(self, node: Any) -> str:
        """First meaningful line of the doc comments immediately preceding *node*."""
        doc = self._config.doc_comment
        if doc is None:
            return ""
        comments: list[Any] = []
        sib = node.prev_sibling
        while sib is not None and sib.type in doc.comment_types:
            comments.append(sib)
            sib = sib.prev_sibling
        if not comments:
            return ""
        comments.reverse()  # back to source order
        return _clean_doc_line("\n".join(_text(c) for c in comments))

    @staticmethod
    def _first_of_types(node: Any, types: tuple[str, ...]) -> Optional[Any]:
        """First descendant of *node* whose type is in *types* (pre-order)."""
        stack = list(node.named_children)
        while stack:
            current = stack.pop(0)
            if current.type in types:
                return current
            stack[:0] = list(current.named_children)
        return None


__all__ = ["TreeSitterBuilder", "module_summary"]
