"""Scope graph — a language-neutral name-resolution model and LEGB resolver.

This is the low, self-contained core of CodeMap's name resolution. It knows
nothing about ``ast`` (the extractor feeds it) and nothing about the store, the
facade, or the repo — it is pure data (:class:`Scope` / :class:`Def` / :class:`Ref`)
plus one resolver (:class:`ScopeGraph`). That isolation is deliberate: it makes
resolution unit-testable without parsing, and leaves a clean seam for a future
second (non-Python) provider that would emit the same data model.

The single choke point is :meth:`ScopeGraph.resolve` — a Python-semantics LEGB
walk (Local → Enclosing → Global → Builtin) with the three corrections a
scope-*blind* name set gets wrong:

- **shadowing** — a param / local / import named like a module-level ``def``
  resolves to the nearer binding, so it is *not* misread as a call to the def;
- **class-scope skip** — a method body cannot see class-level names (Python does
  not put the class namespace on a method's lookup chain), but a ref that
  *originates* in the class body itself can;
- **global / nonlocal** — an explicit declaration redirects the lookup to the
  module scope (``global``) or the nearest enclosing function scope (``nonlocal``).

``self`` / ``cls`` attribute calls (``self.foo()``) are modeled with
:attr:`Ref.via_self`: they resolve in the nearest enclosing *class* scope (the
one place a method body legitimately reaches a sibling method), which the plain
class-skip walk would otherwise miss.

:meth:`ScopeGraph.call_edges` builds intra-file caller→callee edges off the same
resolver, attributing each call to the *single* function/class/module scope that
owns the call site (no double-attribution into nested defs) and keeping only
edges whose callee is a locally-defined function/class (dropping builtins,
imports, params, and unresolved names).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

#: A lexical scope's kind. ``comprehension`` / ``lambda`` introduce their own
#: name scopes in Python 3; they are transparent to call-edge *attribution*
#: (a call inside them belongs to the enclosing function) but matter for
#: resolution (their iteration vars / params bind locally). ``block`` is the
#: language-neutral lexical block (a C/Java/JS ``{ … }``, a Rust block) — like
#: comprehension/lambda it is a *non-owning* intermediate scope: a call inside it
#: attributes to the enclosing function, but a block-local binding still shadows
#: an outer one in the LEGB walk.
ScopeKind = Literal["module", "function", "class", "comprehension", "lambda", "block"]

#: Scope kinds that *own* a call site for attribution. A ref inside a
#: comprehension/lambda/block is attributed to the nearest enclosing one of these.
_OWNING_KINDS = frozenset({"module", "function", "class"})

#: Def kinds a call edge may point at — a real local definition. A call whose
#: resolved binding is a param/variable/import is not a same-file symbol edge.
_CALLABLE_KINDS = frozenset({"function", "class"})


@dataclass
class Scope:
    """A lexical scope: module, function, class, comprehension, or lambda."""

    id: int
    kind: ScopeKind
    parent: Optional[int]  # enclosing scope id; None for the module scope
    start_line: int


@dataclass
class Def:
    """A name binding introduced in a scope (def/class/assign/param/import).

    ``scope`` is where the name is *bound* (the enclosing scope); ``body_scope``
    is the scope a ``def``/``class`` *introduces* (its body), used to map a call
    site's owning scope back to the symbol that owns it. ``is_global`` /
    ``is_nonlocal`` mark a ``global x`` / ``nonlocal x`` declaration — such a Def
    is not itself a binding in ``scope`` (it redirects the lookup elsewhere).
    """

    name: str
    scope: int
    line: int
    kind: Literal["function", "class", "variable", "param", "import"]
    qualified_name: str = ""
    is_global: bool = False
    is_nonlocal: bool = False
    body_scope: Optional[int] = None


@dataclass
class Ref:
    """A name use site: a bare load or a call head (``foo`` in ``foo()``)."""

    name: str
    scope: int
    line: int
    col: int
    is_call: bool = False
    via_self: bool = False  # ``self.name`` / ``cls.name`` attribute access


@dataclass
class ScopeGraph:
    """Scopes + defs + refs for one file, with the LEGB resolver over them.

    Two per-language policy knobs keep the resolver itself language-neutral:

    - ``skip_class_scope`` — Python semantics skip the class namespace on a
      method's lookup chain (a method body cannot see class-level names by bare
      name), so it defaults ``True``. Most other languages (C-family / Java / JS)
      let a method reach sibling members by bare name, so their providers pass
      ``False`` and the class scope participates in the ordinary LEGB walk. This
      is the one bit persisted per file (the store round-trips it).
    - ``self_receivers`` — the receiver tokens whose ``recv.member()`` resolves in
      the enclosing class scope (Python ``{self, cls}``, JS/Java/C++ ``{this}``,
      Rust ``{self}``). Consumed at *build* time only (a builder stamps
      :attr:`Ref.via_self`), so it need not persist.
    """

    scopes: dict[int, Scope] = field(default_factory=dict)
    defs: list[Def] = field(default_factory=list)
    refs: list[Ref] = field(default_factory=list)
    skip_class_scope: bool = True
    self_receivers: frozenset[str] = frozenset()

    # -- resolution ----------------------------------------------------------

    def resolve(self, ref: Ref) -> Optional[Def]:
        """The :class:`Def` *ref* binds to, or None (builtin / external / unknown).

        Honors, in order: ``self``/``cls`` attribute access (nearest class
        scope), an explicit ``global`` (module scope) / ``nonlocal`` (nearest
        enclosing function scope) declaration in the ref's own scope, then a
        plain LEGB walk with class scopes skipped as *intermediate* ancestors.
        """
        if ref.via_self:
            cls_scope = self._enclosing_class(ref.scope)
            if cls_scope is None:
                return None
            return self._def_in(ref.name, cls_scope)

        decl = self._declaration(ref.name, ref.scope)
        if decl == "global":
            module_scope = self._module_scope_id()
            return None if module_scope is None else self._def_in(ref.name, module_scope)
        if decl == "nonlocal":
            return self._nonlocal_binding(ref.name, ref.scope)

        return self._legb(ref.name, ref.scope)

    def call_edges(self) -> list[tuple[Optional[Def], Def, int]]:
        """``(owner_def_or_None, callee_def, line)`` for each resolvable call.

        ``owner`` is the def of the single function/class/module scope that owns
        the call site (None at module level) — so a call inside a nested def is
        attributed to that def *once*, never also to its ancestors. Only calls
        whose callee resolves to a locally-defined function/class are kept.
        """
        edges: list[tuple[Optional[Def], Def, int]] = []
        for ref in self.refs:
            if not ref.is_call:
                continue
            callee = self.resolve(ref)
            if callee is None or callee.kind not in _CALLABLE_KINDS:
                continue
            edges.append((self._owning_def(ref.scope), callee, ref.line))
        return edges

    # -- lookup internals ----------------------------------------------------

    def _legb(self, name: str, origin: Optional[int]) -> Optional[Def]:
        """LEGB walk from *origin* upward; class scopes skipped unless the origin."""
        current = origin
        is_origin = True
        while current is not None:
            scope = self.scopes.get(current)
            if scope is None:
                return None
            if self.skip_class_scope and scope.kind == "class" and not is_origin:
                current = scope.parent
                is_origin = False
                continue
            found = self._def_in(name, current)
            if found is not None:
                return found
            current = scope.parent
            is_origin = False
        return None

    def _def_in(self, name: str, scope_id: int) -> Optional[Def]:
        """First real binding of *name* in *scope_id* (global/nonlocal skipped)."""
        for d in self.defs:
            if d.scope == scope_id and d.name == name and not d.is_global and not d.is_nonlocal:
                return d
        return None

    def _declaration(self, name: str, scope_id: Optional[int]) -> Optional[str]:
        """ "global" / "nonlocal" if *name* is so declared in *scope_id*, else None."""
        for d in self.defs:
            if d.scope == scope_id and d.name == name:
                if d.is_global:
                    return "global"
                if d.is_nonlocal:
                    return "nonlocal"
        return None

    def _nonlocal_binding(self, name: str, origin: int) -> Optional[Def]:
        """Nearest *enclosing* function scope that binds *name* (nonlocal target)."""
        scope = self.scopes.get(origin)
        current = scope.parent if scope else None
        while current is not None:
            s = self.scopes.get(current)
            if s is None:
                return None
            if s.kind == "function":
                found = self._def_in(name, current)
                if found is not None:
                    return found
            current = s.parent
        return None

    def _enclosing_class(self, scope_id: Optional[int]) -> Optional[int]:
        """The nearest class scope at or above *scope_id* (for ``self.``)."""
        current = scope_id
        while current is not None:
            scope = self.scopes.get(current)
            if scope is None:
                return None
            if scope.kind == "class":
                return current
            current = scope.parent
        return None

    def _owning_scope(self, scope_id: Optional[int]) -> Optional[int]:
        """Nearest function/class/module scope at or above *scope_id*."""
        current = scope_id
        while current is not None:
            scope = self.scopes.get(current)
            if scope is None:
                return None
            if scope.kind in _OWNING_KINDS:
                return current
            current = scope.parent
        return None

    def _owning_def(self, scope_id: int) -> Optional[Def]:
        """The def whose body is the owning scope of *scope_id* (None at module)."""
        owning = self._owning_scope(scope_id)
        if owning is None:
            return None
        for d in self.defs:
            if d.body_scope == owning:
                return d
        return None  # module scope owns no def

    def _module_scope_id(self) -> Optional[int]:
        for scope_id, scope in self.scopes.items():
            if scope.kind == "module":
                return scope_id
        return None


__all__ = ["Scope", "Def", "Ref", "ScopeGraph", "ScopeKind"]
