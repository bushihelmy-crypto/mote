"""Tests for the scope-graph resolver — LEGB, class-skip, shadowing, call edges.

These drive the resolver purely on the :class:`ScopeGraph` data model (no
``ast``), which is exactly the isolation the design buys: name resolution is
verifiable without parsing a real file.
"""

from __future__ import annotations

from mote.runtime.code_map.scopes import Def, Ref, Scope, ScopeGraph


def _module_and_fn(fn_name: str = "run"):
    """A module scope (0) + one function scope (1) whose def is `fn_name`."""
    scopes = {
        0: Scope(id=0, kind="module", parent=None, start_line=1),
        1: Scope(id=1, kind="function", parent=0, start_line=1),
    }
    fn = Def(
        name=fn_name,
        scope=0,
        line=1,
        kind="function",
        qualified_name=fn_name,
        body_scope=1,
    )
    return scopes, fn


# -- LEGB ---------------------------------------------------------------------


def test_local_shadows_module():
    scopes, fn = _module_and_fn()
    g = ScopeGraph(
        scopes=scopes,
        defs=[
            fn,
            Def(name="x", scope=0, line=1, kind="variable", qualified_name="x"),  # module x
            Def(name="x", scope=1, line=2, kind="variable", qualified_name="x"),  # local x shadows
        ],
        refs=[],
    )
    ref = Ref(name="x", scope=1, line=3, col=4)
    got = g.resolve(ref)
    assert got is not None and got.scope == 1  # the local binding wins


def test_enclosing_scope_visible():
    # module has helper(); a ref inside a function resolves up to it.
    scopes, fn = _module_and_fn()
    g = ScopeGraph(
        scopes=scopes,
        defs=[
            fn,
            Def(
                name="helper",
                scope=0,
                line=1,
                kind="function",
                qualified_name="helper",
                body_scope=None,
            ),
        ],
        refs=[],
    )
    got = g.resolve(Ref(name="helper", scope=1, line=2, col=4))
    assert got is not None and got.name == "helper"


def test_unresolved_returns_none():
    scopes, fn = _module_and_fn()
    g = ScopeGraph(scopes=scopes, defs=[fn], refs=[])
    assert g.resolve(Ref(name="print", scope=1, line=2, col=4)) is None


# -- class-scope skip ---------------------------------------------------------


def _class_with_method():
    """module(0) -> class C(1) -> method m(2)."""
    scopes = {
        0: Scope(id=0, kind="module", parent=None, start_line=1),
        1: Scope(id=1, kind="class", parent=0, start_line=1),
        2: Scope(id=2, kind="function", parent=1, start_line=2),
    }
    cls = Def(name="C", scope=0, line=1, kind="class", qualified_name="C", body_scope=1)
    method = Def(name="m", scope=1, line=2, kind="function", qualified_name="C.m", body_scope=2)
    return scopes, cls, method


def test_method_body_cannot_see_class_level_name():
    # A class-level name is NOT on a method's lookup chain (Python semantics).
    scopes, cls, method = _class_with_method()
    g = ScopeGraph(
        scopes=scopes,
        defs=[
            cls,
            method,
            Def(name="attr", scope=1, line=2, kind="variable", qualified_name="C.attr"),
        ],
        refs=[],
    )
    # ref to `attr` from within method m must NOT resolve to the class-level attr.
    assert g.resolve(Ref(name="attr", scope=2, line=3, col=8)) is None


def test_class_body_ref_sees_class_level_name():
    # A ref that ORIGINATES in the class body can see class-level names.
    scopes, cls, method = _class_with_method()
    g = ScopeGraph(
        scopes=scopes,
        defs=[
            cls,
            method,
            Def(name="attr", scope=1, line=2, kind="variable", qualified_name="C.attr"),
        ],
        refs=[],
    )
    got = g.resolve(Ref(name="attr", scope=1, line=3, col=4))
    assert got is not None and got.name == "attr"


def test_via_self_resolves_sibling_method():
    # self.helper() from within a method resolves to the sibling method.
    scopes = {
        0: Scope(id=0, kind="module", parent=None, start_line=1),
        1: Scope(id=1, kind="class", parent=0, start_line=1),
        2: Scope(id=2, kind="function", parent=1, start_line=2),  # helper
        3: Scope(id=3, kind="function", parent=1, start_line=4),  # run
    }
    helper = Def(
        name="helper",
        scope=1,
        line=2,
        kind="function",
        qualified_name="C.helper",
        body_scope=2,
    )
    run = Def(
        name="run",
        scope=1,
        line=4,
        kind="function",
        qualified_name="C.run",
        body_scope=3,
    )
    cls = Def(name="C", scope=0, line=1, kind="class", qualified_name="C", body_scope=1)
    g = ScopeGraph(scopes=scopes, defs=[cls, helper, run], refs=[])
    got = g.resolve(Ref(name="helper", scope=3, line=5, col=8, is_call=True, via_self=True))
    assert got is not None and got.qualified_name == "C.helper"


# -- global / nonlocal --------------------------------------------------------


def test_global_redirects_to_module():
    scopes, fn = _module_and_fn()
    g = ScopeGraph(
        scopes=scopes,
        defs=[
            fn,
            Def(name="x", scope=0, line=1, kind="variable", qualified_name="x"),  # module binding
            Def(name="x", scope=1, line=2, kind="variable", is_global=True),  # `global x` decl
        ],
        refs=[],
    )
    got = g.resolve(Ref(name="x", scope=1, line=3, col=4))
    assert got is not None and got.scope == 0  # module binding, not a local


def test_nonlocal_redirects_to_enclosing_function():
    scopes = {
        0: Scope(id=0, kind="module", parent=None, start_line=1),
        1: Scope(id=1, kind="function", parent=0, start_line=1),  # outer
        2: Scope(id=2, kind="function", parent=1, start_line=2),  # inner
    }
    outer = Def(
        name="outer",
        scope=0,
        line=1,
        kind="function",
        qualified_name="outer",
        body_scope=1,
    )
    inner = Def(
        name="inner",
        scope=1,
        line=2,
        kind="function",
        qualified_name="outer.inner",
        body_scope=2,
    )
    g = ScopeGraph(
        scopes=scopes,
        defs=[
            outer,
            inner,
            Def(name="v", scope=1, line=1, kind="variable"),  # binding in outer
            Def(name="v", scope=2, line=2, kind="variable", is_nonlocal=True),  # `nonlocal v` in inner
        ],
        refs=[],
    )
    got = g.resolve(Ref(name="v", scope=2, line=3, col=8))
    assert got is not None and got.scope == 1  # the enclosing-function binding


# -- call_edges ---------------------------------------------------------------


def test_call_edges_single_owner_no_double_attribution():
    # A call inside a nested function is attributed ONLY to that nested function,
    # never also to its enclosing function.
    scopes = {
        0: Scope(id=0, kind="module", parent=None, start_line=1),
        1: Scope(id=1, kind="function", parent=0, start_line=1),  # outer
        2: Scope(id=2, kind="function", parent=1, start_line=2),  # inner
    }
    outer = Def(
        name="outer",
        scope=0,
        line=1,
        kind="function",
        qualified_name="outer",
        body_scope=1,
    )
    inner = Def(
        name="inner",
        scope=1,
        line=2,
        kind="function",
        qualified_name="outer.inner",
        body_scope=2,
    )
    target = Def(
        name="target",
        scope=0,
        line=5,
        kind="function",
        qualified_name="target",
        body_scope=None,
    )
    g = ScopeGraph(
        scopes=scopes,
        defs=[outer, inner, target],
        refs=[Ref(name="target", scope=2, line=3, col=8, is_call=True)],
    )
    edges = [(o.qualified_name if o else "", c.name) for o, c, _ in g.call_edges()]
    assert edges == [("outer.inner", "target")]  # single owner


def test_call_edges_drops_shadowed_param():
    # A param named like a module def must NOT produce a call edge.
    scopes, fn = _module_and_fn()
    g = ScopeGraph(
        scopes=scopes,
        defs=[
            fn,
            Def(
                name="helper",
                scope=0,
                line=1,
                kind="function",
                qualified_name="helper",
                body_scope=None,
            ),
            Def(name="helper", scope=1, line=1, kind="param", qualified_name="helper"),  # param shadows
        ],
        refs=[Ref(name="helper", scope=1, line=2, col=4, is_call=True)],
    )
    assert g.call_edges() == []  # callee resolves to a param -> dropped


def test_call_edges_module_level_owner_is_none():
    scopes, fn = _module_and_fn()
    g = ScopeGraph(
        scopes=scopes,
        defs=[
            fn,
            Def(
                name="helper",
                scope=0,
                line=1,
                kind="function",
                qualified_name="helper",
                body_scope=None,
            ),
        ],
        refs=[Ref(name="helper", scope=0, line=3, col=0, is_call=True)],  # call at module level
    )
    edges = g.call_edges()
    assert len(edges) == 1
    owner, callee, _ = edges[0]
    assert owner is None and callee.name == "helper"


def test_call_edges_drops_unresolved():
    scopes, fn = _module_and_fn()
    g = ScopeGraph(
        scopes=scopes,
        defs=[fn],
        refs=[Ref(name="print", scope=1, line=2, col=4, is_call=True)],
    )
    assert g.call_edges() == []


# -- block scopes (language-neutral lexical blocks) ---------------------------


def test_block_local_shadow_wins():
    # A binding inside a `block` scope shadows the enclosing-function binding for
    # a ref that originates in the block.
    scopes = {
        0: Scope(id=0, kind="module", parent=None, start_line=1),
        1: Scope(id=1, kind="function", parent=0, start_line=1),
        2: Scope(id=2, kind="block", parent=1, start_line=2),
    }
    fn = Def(name="run", scope=0, line=1, kind="function", qualified_name="run", body_scope=1)
    g = ScopeGraph(
        scopes=scopes,
        defs=[
            fn,
            Def(name="x", scope=1, line=1, kind="variable", qualified_name="x"),  # function-local
            Def(name="x", scope=2, line=2, kind="variable", qualified_name="x"),  # block-local shadows
        ],
        refs=[],
    )
    got = g.resolve(Ref(name="x", scope=2, line=3, col=4))
    assert got is not None and got.scope == 2  # block-local wins


def test_block_never_owns_a_call_edge():
    # A call inside a block attributes to the enclosing FUNCTION, not the block
    # (block is a non-owning scope).
    scopes = {
        0: Scope(id=0, kind="module", parent=None, start_line=1),
        1: Scope(id=1, kind="function", parent=0, start_line=1),
        2: Scope(id=2, kind="block", parent=1, start_line=2),
    }
    fn = Def(name="run", scope=0, line=1, kind="function", qualified_name="run", body_scope=1)
    target = Def(
        name="target",
        scope=0,
        line=5,
        kind="function",
        qualified_name="target",
        body_scope=None,
    )
    g = ScopeGraph(
        scopes=scopes,
        defs=[fn, target],
        refs=[Ref(name="target", scope=2, line=3, col=8, is_call=True)],
    )
    edges = [(o.qualified_name if o else "", c.name) for o, c, _ in g.call_edges()]
    assert edges == [("run", "target")]  # attributed to the enclosing function


# -- skip_class_scope=False (non-Python member resolution) --------------------


def test_skip_class_scope_false_resolves_sibling_member():
    # With skip_class_scope=False (C-family/JS/Java), a method body reaches a
    # sibling class member by bare name via the ordinary LEGB walk.
    scopes, cls, method = _class_with_method()
    g = ScopeGraph(
        scopes=scopes,
        defs=[
            cls,
            method,
            Def(
                name="sibling",
                scope=1,
                line=2,
                kind="function",
                qualified_name="C.sibling",
            ),
        ],
        refs=[],
        skip_class_scope=False,
    )
    got = g.resolve(Ref(name="sibling", scope=2, line=3, col=8))
    assert got is not None and got.name == "sibling"


def test_skip_class_scope_true_is_default_and_hides_member():
    # The default (Python) still hides class-level names from a method body.
    scopes, cls, method = _class_with_method()
    g = ScopeGraph(
        scopes=scopes,
        defs=[
            cls,
            method,
            Def(
                name="sibling",
                scope=1,
                line=2,
                kind="function",
                qualified_name="C.sibling",
            ),
        ],
        refs=[],
    )
    assert g.skip_class_scope is True
    assert g.resolve(Ref(name="sibling", scope=2, line=3, col=8)) is None
