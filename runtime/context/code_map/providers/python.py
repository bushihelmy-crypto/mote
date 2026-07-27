"""PythonProvider — the ``ast``-based :class:`LanguageProvider` for ``.py``.

This holds everything language-specific about parsing Python that used to live on
``CodeMapExtractor``: the one scope-aware AST walk (:class:`_ScopeGraphBuilder`)
that emits a :class:`~mote.runtime.context.code_map.scopes.ScopeGraph` + presentation
symbols, plus the import collectors and the docstring/signature helpers. It is
moved *verbatim* from the old extractor — the extractor is now a thin,
language-agnostic dispatch facade that owns only I/O + freshness and delegates
per-file parsing here.

Why ``ast`` and not tree-sitter for Python: the stdlib ``ast`` is the most
accurate Python parser there is — it *is* CPython's — with zero new dependency,
so the Python provider stays on it while every other language plugs in through
the tree-sitter provider.

Best-effort contract: :meth:`extract_tree` returns ``None`` on a syntax error (the
facade turns that into an empty extract) and never raises.
"""

from __future__ import annotations

import ast
from typing import Optional

from mote.runtime.context.code_map.model import SUMMARY_MAX_CHARS, ImportBinding, ImportRef, Symbol
from mote.runtime.context.code_map.providers.base import LanguageProvider, ModuleResolver, ProviderExtract
from mote.runtime.context.code_map.providers.resolvers.python import PythonModuleResolver
from mote.runtime.context.code_map.scopes import Def, Ref, Scope, ScopeGraph

#: Attribute-call receivers whose ``.foo()`` unambiguously targets a same-file
#: method: ``self`` / ``cls`` bind to the enclosing class, so ``self.foo()`` is a
#: real method call. Any other receiver (``x.foo()``, ``mod.foo()``) names an
#: object we cannot type from the AST alone, so bare-name matching there would be
#: a false positive — no ref is emitted for it.
_SELF_RECEIVERS = frozenset({"self", "cls"})


class PythonProvider:
    """Parses one Python file into a :class:`ProviderExtract` (structure only)."""

    def __init__(self) -> None:
        self._resolver = PythonModuleResolver()

    @property
    def language(self) -> str:
        return "python"

    @property
    def extensions(self) -> tuple[str, ...]:
        return (".py",)

    def module_resolver(self) -> ModuleResolver:
        return self._resolver

    def extract_tree(self, source: str, abspath: str) -> Optional[ProviderExtract]:
        """Parse *source* into the neutral extract, or ``None`` on a syntax error."""
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            return None

        # One walk builds the scope graph AND the presentation symbols.
        builder = _ScopeGraphBuilder()
        graph, symbols = builder.build(tree)

        # The importing file's own package chain, so a relative import can be
        # rewritten to the absolute dotted name it actually targets (recall).
        pkg = self._resolver.package_segments(abspath)
        return ProviderExtract(
            module_summary=_docstring_line(tree),
            symbols=symbols,
            scope_graph=graph,
            imports=self._collect_imports(tree, pkg),
            import_refs=self._collect_import_refs(tree, pkg),
            import_bindings=self._collect_import_bindings(tree, pkg),
        )

    # -- imports -------------------------------------------------------------

    def _collect_imports(self, tree: ast.AST, pkg: Optional[list[str]]) -> list[str]:
        """Module names from ``import`` / ``from ... import``; de-duped, order-kept.

        Relative imports are resolved to their absolute dotted target using the
        importing file's own package chain (*pkg*) when it can be anchored, so a
        reverse-dep / dangling query can match them the same as an absolute
        import. An unanchorable relative falls back to the dotted-prefixed
        spelling (``.other``), preserving the previous behavior.
        """
        seen: set[str] = set()
        modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name not in seen:
                        seen.add(alias.name)
                        modules.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                mod = self._import_from_module(node, pkg)
                if mod and mod not in seen:
                    seen.add(mod)
                    modules.append(mod)
        return modules

    def _collect_import_refs(self, tree: ast.AST, pkg: Optional[list[str]]) -> list[ImportRef]:
        """Per-binding import reference sites, with (line, col) of each name."""
        refs: list[ImportRef] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    refs.append(
                        ImportRef(
                            module=alias.name,
                            name=alias.asname or alias.name,
                            line=getattr(alias, "lineno", node.lineno),
                            col=getattr(alias, "col_offset", node.col_offset),
                            imported_name="",  # whole-module import binds no symbol
                        )
                    )
            elif isinstance(node, ast.ImportFrom):
                mod = self._import_from_module(node, pkg)
                for alias in node.names:
                    refs.append(
                        ImportRef(
                            module=mod,
                            name=alias.asname or alias.name,
                            line=getattr(alias, "lineno", node.lineno),
                            col=getattr(alias, "col_offset", node.col_offset),
                            imported_name=alias.name,
                        )
                    )
        return refs

    def _collect_import_bindings(self, tree: ast.AST, pkg: Optional[list[str]]) -> list[ImportBinding]:
        """Symbol-level bindings: ``local_name = imported_name @ module``.

        A ``from a.b import c as d`` yields ``ImportBinding("d", "a.b", "c")`` —
        the seam a cross-file reverse-dep query matches on. A whole-module
        ``import a.b.c`` yields ``ImportBinding("a", "a.b.c", "")`` (binds the top
        package, no symbol); ``import a.b as x`` binds ``x``. Star imports are
        skipped (they name no binding).
        """
        bindings: list[ImportBinding] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name.split(".")[0]
                    bindings.append(
                        ImportBinding(local_name=local, module=alias.name, imported_name="", line=node.lineno)
                    )
            elif isinstance(node, ast.ImportFrom):
                mod = self._import_from_module(node, pkg)
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    local = alias.asname or alias.name
                    bindings.append(
                        ImportBinding(local_name=local, module=mod, imported_name=alias.name, line=node.lineno)
                    )
        return bindings

    def _import_from_module(self, node: ast.ImportFrom, pkg: Optional[list[str]]) -> str:
        """Dotted target of a ``from ... import``, relatives resolved when anchorable."""
        if node.level == 0:
            return node.module or ""
        if node.module:
            resolved = self._resolver.resolve_relative(pkg, node.level, node.module)
            if resolved is not None:
                return resolved
        return ("." * node.level) + (node.module or "")


# -- shared presentation helpers (module-level; used by the builder) ---------


def _docstring_line(node) -> str:
    """First meaningful line of *node*'s docstring, trimmed — else ""."""
    try:
        doc = ast.get_docstring(node, clean=True)
    except TypeError:
        return ""  # node kind ast.get_docstring doesn't accept
    if not doc:
        return ""
    first = ""
    for line in doc.splitlines():
        stripped = line.strip()
        if stripped:
            first = stripped
            break
    if not first:
        return ""
    first = " ".join(first.split())  # collapse internal whitespace runs
    if len(first) > SUMMARY_MAX_CHARS:
        first = first[: SUMMARY_MAX_CHARS - 1].rstrip() + "…"
    return first


def _signature(node) -> str:
    """Render ``(params) -> ret`` from a function node, best-effort."""
    try:
        params = ast.unparse(node.args)
    except Exception:  # noqa: BLE001 — unparse is advisory
        params = ""
    sig = f"({params})"
    returns = getattr(node, "returns", None)
    if returns is not None:
        try:
            sig += f" -> {ast.unparse(returns)}"
        except Exception:  # noqa: BLE001
            pass
    return sig


class _ScopeGraphBuilder:
    """One scope-aware AST walk building a :class:`ScopeGraph` + presentation symbols.

    Manual recursion (not :class:`ast.NodeVisitor`) so scope push/pop and the
    qualified-name prefix are threaded explicitly. Within a scope it collects
    name bindings (defs) and use sites (refs); a nested ``def`` / ``class`` /
    ``lambda`` / comprehension opens a child scope. Decorators, default values,
    and annotations are visited in the *enclosing* scope (Python evaluates them
    there), while params bind in the introduced scope.
    """

    def __init__(self) -> None:
        self.scopes: dict[int, Scope] = {}
        self.defs: list[Def] = []
        self.refs: list[Ref] = []
        self.symbols: list[Symbol] = []
        self._next_id = 0

    def build(self, tree: ast.Module) -> tuple[ScopeGraph, list[Symbol]]:
        module_scope = self._new_scope("module", None, 1)
        self._process_body(tree.body, module_scope, prefix="")
        return ScopeGraph(scopes=self.scopes, defs=self.defs, refs=self.refs), self.symbols

    def _new_scope(self, kind, parent: Optional[int], line: int) -> int:
        sid = self._next_id
        self._next_id += 1
        self.scopes[sid] = Scope(id=sid, kind=kind, parent=parent, start_line=line)
        return sid

    def _process_body(self, body: list, scope_id: int, prefix: str) -> None:
        for stmt in body:
            self._visit(stmt, scope_id, prefix)

    def _visit(self, node, scope_id: int, prefix: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self._function(node, scope_id, prefix)
        elif isinstance(node, ast.ClassDef):
            self._class(node, scope_id, prefix)
        elif isinstance(node, ast.Lambda):
            self._lambda(node, scope_id)
        elif isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            self._comprehension(node, scope_id)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                self.defs.append(Def(name=local, scope=scope_id, line=node.lineno, kind="import"))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    continue
                local = alias.asname or alias.name
                self.defs.append(Def(name=local, scope=scope_id, line=node.lineno, kind="import"))
        elif isinstance(node, ast.Global):
            for name in node.names:
                self.defs.append(Def(name=name, scope=scope_id, line=node.lineno, kind="variable", is_global=True))
        elif isinstance(node, ast.Nonlocal):
            for name in node.names:
                self.defs.append(Def(name=name, scope=scope_id, line=node.lineno, kind="variable", is_nonlocal=True))
        elif isinstance(node, ast.Call):
            self._call(node, scope_id, prefix)
        elif isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load):
                self.refs.append(Ref(name=node.id, scope=scope_id, line=node.lineno, col=node.col_offset))
            elif isinstance(node.ctx, (ast.Store, ast.Del)):
                # A bare Store/Del name is a binding: assignment / for / with-as /
                # comprehension target — captured uniformly here.
                self.defs.append(Def(name=node.id, scope=scope_id, line=node.lineno, kind="variable"))
        else:
            # Generic node (compound statement, expression container). Recurse into
            # children in the SAME scope so nested defs inside if/for/try/with are
            # still opened here, and inner Name loads/stores are captured.
            for child in ast.iter_child_nodes(node):
                self._visit(child, scope_id, prefix)

    def _call(self, node: ast.Call, scope_id: int, prefix: str) -> None:
        func = node.func
        if isinstance(func, ast.Name) and isinstance(func.ctx, ast.Load):
            self.refs.append(Ref(name=func.id, scope=scope_id, line=func.lineno, col=func.col_offset, is_call=True))
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in _SELF_RECEIVERS:
            self.refs.append(
                Ref(name=func.attr, scope=scope_id, line=func.lineno, col=func.col_offset, is_call=True, via_self=True)
            )
        else:
            # Foreign / complex receiver — no same-file call ref; still visit the
            # callee expression to capture any inner refs (e.g. the receiver name).
            self._visit(func, scope_id, prefix)
        for arg in node.args:
            self._visit(arg, scope_id, prefix)
        for kw in node.keywords:
            self._visit(kw.value, scope_id, prefix)

    def _function(self, node, scope_id: int, prefix: str) -> None:
        qualified = f"{prefix}{node.name}"
        kind = "method" if prefix else "function"
        body_scope = self._new_scope("function", scope_id, node.lineno)
        self.symbols.append(
            Symbol(
                name=node.name,
                qualified_name=qualified,
                kind=kind,
                start_line=node.lineno,
                signature=_signature(node),
                summary=_docstring_line(node),
            )
        )
        self.defs.append(
            Def(
                name=node.name,
                scope=scope_id,
                line=node.lineno,
                kind="function",
                qualified_name=qualified,
                body_scope=body_scope,
            )
        )
        # Decorators, defaults, and annotations evaluate in the enclosing scope.
        for dec in node.decorator_list:
            self._visit(dec, scope_id, prefix)
        self._visit_defaults(node.args, scope_id, prefix)
        if getattr(node, "returns", None) is not None:
            self._visit(node.returns, scope_id, prefix)
        # Params bind in the function's own scope.
        for arg in self._all_args(node.args):
            self.defs.append(Def(name=arg.arg, scope=body_scope, line=arg.lineno, kind="param", qualified_name=arg.arg))
            if arg.annotation is not None:
                self._visit(arg.annotation, scope_id, prefix)
        self._process_body(node.body, body_scope, prefix=f"{qualified}.")

    def _class(self, node: ast.ClassDef, scope_id: int, prefix: str) -> None:
        qualified = f"{prefix}{node.name}"
        body_scope = self._new_scope("class", scope_id, node.lineno)
        self.symbols.append(
            Symbol(
                name=node.name,
                qualified_name=qualified,
                kind="class",
                start_line=node.lineno,
                signature="",
                summary=_docstring_line(node),
            )
        )
        self.defs.append(
            Def(
                name=node.name,
                scope=scope_id,
                line=node.lineno,
                kind="class",
                qualified_name=qualified,
                body_scope=body_scope,
            )
        )
        for dec in node.decorator_list:
            self._visit(dec, scope_id, prefix)
        for base in node.bases:
            self._visit(base, scope_id, prefix)
        for kw in node.keywords:
            self._visit(kw.value, scope_id, prefix)
        self._process_body(node.body, body_scope, prefix=f"{qualified}.")

    def _lambda(self, node: ast.Lambda, scope_id: int) -> None:
        body_scope = self._new_scope("lambda", scope_id, node.lineno)
        self._visit_defaults(node.args, scope_id, "")
        for arg in self._all_args(node.args):
            self.defs.append(Def(name=arg.arg, scope=body_scope, line=node.lineno, kind="param"))
        self._visit(node.body, body_scope, "")

    def _comprehension(self, node, scope_id: int) -> None:
        body_scope = self._new_scope("comprehension", scope_id, node.lineno)
        for i, gen in enumerate(node.generators):
            # The outermost iterable is evaluated in the ENCLOSING scope; the rest
            # (and all targets/conditions) live in the comprehension's own scope.
            self._visit(gen.iter, scope_id if i == 0 else body_scope, "")
            self._visit(gen.target, body_scope, "")
            for cond in gen.ifs:
                self._visit(cond, body_scope, "")
        if isinstance(node, ast.DictComp):
            self._visit(node.key, body_scope, "")
            self._visit(node.value, body_scope, "")
        else:
            self._visit(node.elt, body_scope, "")

    def _visit_defaults(self, args: ast.arguments, scope_id: int, prefix: str) -> None:
        for d in args.defaults:
            self._visit(d, scope_id, prefix)
        for d in args.kw_defaults:
            if d is not None:
                self._visit(d, scope_id, prefix)

    @staticmethod
    def _all_args(args: ast.arguments) -> list[ast.arg]:
        collected: list[ast.arg] = []
        collected.extend(getattr(args, "posonlyargs", []) or [])
        collected.extend(args.args)
        if args.vararg is not None:
            collected.append(args.vararg)
        collected.extend(args.kwonlyargs)
        if args.kwarg is not None:
            collected.append(args.kwarg)
        return collected


# A module-level instance is fine — PythonProvider is stateless (the resolver it
# holds is pure arithmetic). The registry hands this out per extension.
_PROVIDER: LanguageProvider = PythonProvider()

__all__ = ["PythonProvider", "PythonModuleResolver"]
