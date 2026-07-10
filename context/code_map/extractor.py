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


@dataclass
class FileExtract:
    """Everything the extractor derived from one file."""

    path: str  # absolute path
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)  # imported module names
    calls: list[CallEdge] = field(default_factory=list)  # intra-file symbol->symbol


class CodeMapExtractor:
    """Parses Python files into :class:`FileExtract`, with an mtime freshness cache."""

    def __init__(self) -> None:
        # abspath -> mtime_ns at last successful (or attempted) parse. Used by
        # ensure_fresh to decide whether a re-parse is needed.
        self._mtime: dict[str, int] = {}

    def needs_refresh(self, path: str) -> bool:
        """True if *path* changed on disk since we last parsed it (or never did)."""
        current = self._current_mtime(path)
        if current is None:
            return False  # gone / unreadable — nothing to refresh
        return self._mtime.get(os.path.abspath(path)) != current

    def extract(self, path: str) -> FileExtract:
        """Parse *path* and record its mtime. Best-effort — empty extract on failure.

        Always stamps the mtime cache (even on parse failure) so a broken file is
        not re-parsed every turn until it changes again.
        """
        abspath = os.path.abspath(path)
        current = self._current_mtime(abspath)
        if current is not None:
            self._mtime[abspath] = current

        source = self._read(abspath)
        if source is None:
            return FileExtract(path=abspath)

        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            return FileExtract(path=abspath)

        extract = FileExtract(path=abspath)
        self._defined_names: set[str] = set()
        # First pass: collect symbols + the set of names defined in this file, so
        # the call pass can keep only edges whose callee is a local definition.
        self._walk_symbols(tree.body, prefix="", out=extract)
        # Second pass: intra-file call edges.
        self._walk_calls(tree.body, enclosing="", out=extract)
        # Imports (module-level and nested are both surfaced).
        extract.imports = self._collect_imports(tree)
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

    @staticmethod
    def _callee_name(func: ast.AST) -> Optional[str]:
        """Bare name of a call target: ``foo(...)`` -> "foo", ``x.foo(...)`` -> "foo"."""
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
        return None

    # -- imports -------------------------------------------------------------

    @staticmethod
    def _collect_imports(tree: ast.AST) -> list[str]:
        """Module names from ``import`` / ``from ... import``; de-duped, order-kept."""
        seen: set[str] = set()
        modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name not in seen:
                        seen.add(alias.name)
                        modules.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                # level>0 is a relative import; prefix dots so it's distinguishable.
                mod = ("." * node.level) + (node.module or "")
                if mod and mod not in seen:
                    seen.add(mod)
                    modules.append(mod)
        return modules

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
    def _current_mtime(path: str) -> Optional[int]:
        try:
            return os.stat(path).st_mtime_ns
        except OSError:
            return None

    @staticmethod
    def _read(path: str) -> Optional[str]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except (OSError, UnicodeDecodeError):
            return None


__all__ = ["CodeMapExtractor", "FileExtract", "Symbol", "CallEdge"]
