"""CodeMapExtractor — parse a single Python file into symbols + structural edges.

The extractor is the *structure* half of the local code map: given one Python
source file it derives, purely from the ``ast``, the symbols it defines
(functions / classes / methods) and two kinds of edge:

- **imports** — ``import x`` / ``from a.b import c`` targets (module names), so the
  map can say "this file depends on those modules";
- **calls** — an intra-file ``call`` whose callee name is defined *in the same
  file* becomes a ``symbol -> symbol`` edge, so the map can say "method() calls
  foo()" without any cross-file resolution.

Why ``ast`` and not tree-sitter: the first version is Python-only (multi-language
is an explicit non-goal), and the stdlib ``ast`` is the most accurate Python
parser there is — it *is* CPython's — with zero new dependency. tree-sitter's one
advantage is polyglot parsing, which we deliberately do not need here.

Freshness: the extractor caches ``{path: mtime_ns}`` of the last successful parse
so :meth:`ensure_fresh` can skip files that have not changed on disk since. This
is the lazy, touched-set-driven replacement for a whole-repo file watcher — we
only ever parse a file the agent has actually worked with.

Best-effort throughout: unreadable files, syntax errors, and non-Python paths
yield an empty :class:`FileExtract` and are never raised — a code map that
silently omits a file it could not parse is correct; one that breaks a turn is
not.
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from typing import Optional

from mote.common.disk import mtime_ns
from mote.common.text import content_hash as _content_hash


@dataclass(frozen=True)
class Symbol:
    """A definition site in a file: a function, method, or class."""

    name: str  # bare name, e.g. "foo" or "method"
    qualified_name: str  # dotted within-file path, e.g. "Baz.method"
    kind: str  # "function" | "method" | "class"
    start_line: int  # 1-based
    signature: str = ""  # params (+ return) for funcs/methods; "" for classes


@dataclass(frozen=True)
class CallEdge:
    """An intra-file call: ``caller`` (qualified) invokes same-file ``callee``."""

    caller: str  # qualified name of the enclosing symbol, or "" at module level
    callee: str  # bare name of the called symbol (defined in this file)
    line: int  # 1-based call site


@dataclass(frozen=True)
class ImportRef:
    """A single imported binding + the source position that names it.

    Unlike :attr:`FileExtract.imports` (bare module strings), an ``ImportRef``
    carries the *binding* (``thing`` in ``from pkg.other import thing``) and the
    ``(line, col)`` of the reference site, so Layer B can point an LSP
    ``textDocument/definition`` query at the exact symbol. For a plain
    ``import a.b.c`` the ``name`` is the bound alias (``a`` or the ``as`` name)
    and ``module`` the dotted target.
    """

    module: str  # dotted import target (leading dots for relative imports)
    name: str  # the imported binding at this site
    line: int  # 1-based
    col: int  # 0-based (LSP-style character offset)


@dataclass
class FileExtract:
    """Everything the extractor derived from one file."""

    path: str  # absolute path
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)  # imported module names
    calls: list[CallEdge] = field(default_factory=list)  # intra-file symbol->symbol
    # Import *reference sites* (binding + position) — parallel to ``imports`` but
    # richer. NOT persisted in the store (Layer B resolves these live at render);
    # rides on the in-memory extract only.
    import_refs: list[ImportRef] = field(default_factory=list)
    # sha256 hex of the parsed source bytes ("" when unreadable). Drives the
    # persistent store's staleness diff (content-hash incremental re-parse).
    content_hash: str = ""


class CodeMapExtractor:
    """Parses Python files into :class:`FileExtract`, with an mtime freshness cache."""

    def __init__(self) -> None:
        # abspath -> mtime_ns at last successful (or attempted) parse. Used by
        # ensure_fresh to decide whether a re-parse is needed.
        self._mtime: dict[str, int] = {}

    def needs_refresh(self, path: str) -> bool:
        """True if *path* changed on disk since we last parsed it (or never did)."""
        current = mtime_ns(path)
        if current is None:
            return False  # gone / unreadable — nothing to refresh
        return self._mtime.get(os.path.abspath(path)) != current

    def extract(self, path: str) -> FileExtract:
        """Parse *path* and record its mtime. Best-effort — empty extract on failure.

        Always stamps the mtime cache (even on parse failure) so a broken file is
        not re-parsed every turn until it changes again.
        """
        abspath = os.path.abspath(path)
        current = mtime_ns(abspath)
        if current is not None:
            self._mtime[abspath] = current

        source = self._read(abspath)
        if source is None:
            return FileExtract(path=abspath)

        content_hash = _content_hash(source)

        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            # A broken file still gets a stable content hash so the persistent
            # store's staleness diff sees it as parsed-at-this-version.
            return FileExtract(path=abspath, content_hash=content_hash)

        extract = FileExtract(path=abspath, content_hash=content_hash)
        self._defined_names: set[str] = set()
        # First pass: collect symbols + the set of names defined in this file, so
        # the call pass can keep only edges whose callee is a local definition.
        self._walk_symbols(tree.body, prefix="", out=extract)
        # Second pass: intra-file call edges.
        self._walk_calls(tree.body, enclosing="", out=extract)
        # The importing file's own package chain, so a relative import can be
        # rewritten to the absolute dotted name it actually targets (recall).
        pkg = self._package_segments(abspath)
        # Imports (module-level and nested are both surfaced).
        extract.imports = self._collect_imports(tree, pkg)
        extract.import_refs = self._collect_import_refs(tree, pkg)
        return extract

    # -- symbol pass ---------------------------------------------------------

    def _walk_symbols(self, body: list, prefix: str, out: FileExtract) -> None:
        """Recurse the AST body, emitting a Symbol per def/class (methods nested)."""
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified = f"{prefix}{node.name}"
                kind = "method" if prefix else "function"
                out.symbols.append(
                    Symbol(
                        name=node.name,
                        qualified_name=qualified,
                        kind=kind,
                        start_line=node.lineno,
                        signature=self._signature(node),
                    )
                )
                self._defined_names.add(node.name)
                # Nested defs (closures, methods) qualify under this one.
                self._walk_symbols(node.body, prefix=f"{qualified}.", out=out)
            elif isinstance(node, ast.ClassDef):
                qualified = f"{prefix}{node.name}"
                out.symbols.append(
                    Symbol(
                        name=node.name,
                        qualified_name=qualified,
                        kind="class",
                        start_line=node.lineno,
                        signature="",
                    )
                )
                self._defined_names.add(node.name)
                self._walk_symbols(node.body, prefix=f"{qualified}.", out=out)

    # -- call pass -----------------------------------------------------------

    def _walk_calls(self, body: list, enclosing: str, out: FileExtract) -> None:
        """Emit a CallEdge for each call whose callee is defined in this file."""
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualified = f"{enclosing}{node.name}"
                # Scan this scope's own statements for calls attributed to it, then
                # recurse so nested defs attribute their calls to themselves.
                self._scan_calls_in_scope(node.body, caller=qualified, out=out)
                self._walk_calls(node.body, enclosing=f"{qualified}.", out=out)
            else:
                # Module-level statement: calls here are attributed to "" (module).
                self._scan_calls_in_scope([node], caller=enclosing.rstrip("."), out=out)

    def _scan_calls_in_scope(self, stmts: list, caller: str, out: FileExtract) -> None:
        """Collect same-file call edges from *stmts*, not descending into nested defs."""
        for stmt in stmts:
            for node in ast.walk(stmt):
                # Do not attribute calls that live inside a nested def to this scope;
                # _walk_calls handles those under their own caller.
                if isinstance(node, ast.Call):
                    callee = self._callee_name(node.func)
                    if callee and callee in self._defined_names:
                        out.calls.append(CallEdge(caller=caller, callee=callee, line=node.lineno))

    # Attribute-call receivers whose ``.foo()`` unambiguously targets a same-file
    # symbol: ``self`` / ``cls`` bind to the enclosing class, so ``self.foo()`` is
    # a real method call on a name defined here. Any other receiver (``x.foo()``,
    # ``mod.foo()``) names an *object we cannot type* from the AST alone, so bare-
    # name matching there is a false positive — we skip it.
    _SELF_RECEIVERS = frozenset({"self", "cls"})

    @classmethod
    def _callee_name(cls, func: ast.AST) -> Optional[str]:
        """Bare name of a *resolvable* same-file call target, else None.

        - ``foo(...)`` (bare :class:`ast.Name`) -> ``"foo"`` — a direct reference
          the call pass then checks against the file's defined names.
        - ``self.foo(...)`` / ``cls.foo(...)`` -> ``"foo"`` — a method call on the
          enclosing class, still a same-file symbol.
        - ``x.foo(...)`` / ``pkg.foo(...)`` -> ``None`` — the receiver is an object
          we cannot type from the AST, so matching ``foo`` by bare name would
          wrongly attribute the call to an unrelated same-named local definition.
        """
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            value = func.value
            if isinstance(value, ast.Name) and value.id in cls._SELF_RECEIVERS:
                return func.attr
            return None
        return None

    # -- imports -------------------------------------------------------------

    @classmethod
    def _collect_imports(cls, tree: ast.AST, pkg: Optional[list[str]]) -> list[str]:
        """Module names from ``import`` / ``from ... import``; de-duped, order-kept.

        Relative imports are resolved to their absolute dotted target using the
        importing file's own package chain (*pkg*) when it can be anchored, so a
        reverse-dep / dangling query can match them the same as an absolute
        import. An unanchorable relative (file not in a package, or a ``level``
        that climbs past the package root) falls back to the dotted-prefixed
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
                mod = cls._import_from_module(node, pkg)
                if mod and mod not in seen:
                    seen.add(mod)
                    modules.append(mod)
        return modules

    @classmethod
    def _collect_import_refs(cls, tree: ast.AST, pkg: Optional[list[str]]) -> list[ImportRef]:
        """Per-binding import reference sites, with (line, col) of each name.

        For ``from pkg.other import thing`` the binding is ``thing`` anchored at
        the ``from`` statement (LSP resolves the alias position best-effort). For
        ``import a.b.c`` the module and binding are both the dotted target. The
        alias node carries the position when available; otherwise the parent
        statement's does. Relative ``module`` fields are resolved to absolute via
        the importing file's package chain (*pkg*) — same recall fix as
        :meth:`_collect_imports`, with the same dotted fallback.
        """
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
                        )
                    )
            elif isinstance(node, ast.ImportFrom):
                mod = cls._import_from_module(node, pkg)
                for alias in node.names:
                    refs.append(
                        ImportRef(
                            module=mod,
                            name=alias.asname or alias.name,
                            line=getattr(alias, "lineno", node.lineno),
                            col=getattr(alias, "col_offset", node.col_offset),
                        )
                    )
        return refs

    @classmethod
    def _import_from_module(cls, node: ast.ImportFrom, pkg: Optional[list[str]]) -> str:
        """Dotted target of a ``from ... import``, relatives resolved when anchorable.

        Absolute (``level == 0``) → ``node.module`` verbatim. Relative
        (``level > 0``) → the absolute dotted name it reaches from the importing
        file's package chain *pkg*: drop ``level - 1`` trailing segments of the
        package, then append ``node.module``. When *pkg* is unknown or ``level``
        climbs past its root, fall back to the dotted-prefixed spelling so the
        target stays distinguishable (previous behavior, no regression).
        """
        if node.level == 0:
            return node.module or ""
        # Only resolve relatives that name a module (``from .other import x``,
        # ``from ..pkg.mod import y``) — those point precisely at a sibling file.
        # The bare ``from . import submodule`` idiom (no module) would resolve
        # only to the *package* (its __init__), an imprecise, low-value edge —
        # so leave it dotted-and-skipped rather than surface __init__ noise.
        if node.module:
            resolved = cls._resolve_relative(pkg, node.level, node.module)
            if resolved is not None:
                return resolved
        return ("." * node.level) + (node.module or "")

    @staticmethod
    def _resolve_relative(pkg: Optional[list[str]], level: int, module: Optional[str]) -> Optional[str]:
        """Absolute dotted name a relative import reaches, or None if unanchorable.

        *pkg* is the importing file's package chain (e.g. ``["a", "b"]`` for a
        module in package ``a.b``). ``from . import x`` (level 1) targets the
        package itself; ``from ..c import x`` (level 2) climbs one package up
        then descends into ``c``. Returns None when the file has no package
        anchor or the climb overshoots the root — the caller then keeps the raw
        dotted spelling.
        """
        if not pkg:
            # No package anchor (top-level module, or non-.py). A relative import
            # here is not resolvable to an absolute name — keep the raw spelling.
            return None
        # level 1 stays in the current package; each extra level climbs one up.
        climb = level - 1
        if climb > len(pkg):
            return None  # climbs past the package root — unanchorable
        base = pkg[: len(pkg) - climb]
        tail = module.split(".") if module else []
        segments = base + tail
        return ".".join(segments)  # "" when both empty (from . import x at root pkg)

    @staticmethod
    def _package_segments(abspath: str) -> Optional[list[str]]:
        """The importing file's package chain, inferred from adjacent ``__init__``.

        Walks up from the file's directory while each level is a package (has an
        ``__init__.py``), collecting the package directory names outermost-first.
        A file at ``.../repo/a/b/mod.py`` where ``a`` and ``b`` are packages
        yields ``["a", "b"]``. A file not inside any package (no ``__init__.py``
        alongside) yields ``[]`` — it *is* anchorable (a top-level module), just
        with an empty chain. Non-``.py`` paths yield None (no anchor at all).
        """
        if not abspath.endswith(".py"):
            return None
        directory = os.path.dirname(abspath)
        segments: list[str] = []
        d = directory
        while os.path.exists(os.path.join(d, "__init__.py")):
            segments.append(os.path.basename(d))
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
        segments.reverse()  # outermost package first
        return segments

    # -- helpers -------------------------------------------------------------

    @staticmethod
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

    @staticmethod
    def _read(path: str) -> Optional[str]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except (OSError, UnicodeDecodeError):
            return None


__all__ = ["CodeMapExtractor", "FileExtract", "Symbol", "CallEdge", "ImportRef"]
