"""Tests for the config-driven tree-sitter builder (via the JS provider).

The generic builder is language-parameterized; JavaScript is the proving language.
Each assertion targets a builder responsibility that every future language config
reuses: symbol kinds + qualified names, the `this`-receiver same-file call edge,
the bare-name call edge, doc summaries, and the shared import extractor rows.
"""

from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter_language_pack")

from mote.runtime.context.code_map.languages import provider_for  # noqa: E402


def _extract(source: str, abspath: str = "/repo/src/main.js"):
    prov = provider_for(abspath)
    assert prov is not None
    extract = prov.extract_tree(source, abspath)
    assert extract is not None
    return extract


def test_symbols_kinds_and_qualified_names():
    ex = _extract("class Widget {\n" "  render() { return 1; }\n" "}\n" "function top() { return 2; }\n")
    by_qual = {s.qualified_name: s for s in ex.symbols}
    assert by_qual["Widget"].kind == "class"
    assert by_qual["Widget.render"].kind == "method"
    assert by_qual["top"].kind == "function"
    assert by_qual["Widget.render"].signature == "()"


def test_this_receiver_call_edge():
    ex = _extract("class C {\n" "  a() { return this.b(); }\n" "  b() { return 1; }\n" "}\n")
    edges = ex.scope_graph.call_edges()
    pairs = {(edge[0].qualified_name, edge[1].qualified_name) for edge in edges}
    assert ("C.a", "C.b") in pairs


def test_bare_name_call_edge():
    ex = _extract("function helper() { return 1; }\n" "function main() { return helper(); }\n")
    edges = ex.scope_graph.call_edges()
    pairs = {(edge[0].qualified_name, edge[1].qualified_name) for edge in edges}
    assert ("main", "helper") in pairs


def test_foreign_receiver_no_edge():
    # ``obj.method()`` on a non-``this`` receiver is not a same-file edge.
    ex = _extract(
        "function main() {\n"
        "  const obj = make();\n"
        "  return obj.method();\n"
        "}\n"
        "function method() { return 1; }\n"
    )
    edges = ex.scope_graph.call_edges()
    pairs = {(edge[0].qualified_name, edge[1].qualified_name) for edge in edges}
    assert ("main", "method") not in pairs


def test_doc_summary_from_leading_comment():
    ex = _extract("// Renders the widget.\n" "function render() { return 1; }\n")
    by_qual = {s.qualified_name: s for s in ex.symbols}
    assert by_qual["render"].summary == "Renders the widget."


def test_module_summary_from_file_header():
    ex = _extract("// Top of module.\nfunction f() {}\n")
    assert ex.module_summary == "Top of module."


def test_import_rows():
    ex = _extract('import {a, b as c} from "./util";\nfunction f() {}\n')
    assert "/repo/src/util" in ex.imports
    bindings = {(b.local_name, b.imported_name) for b in ex.import_bindings}
    assert ("a", "a") in bindings
    assert ("c", "b") in bindings


def test_syntax_error_degrades_to_none_or_partial():
    # A malformed source must never raise — provider returns an extract (possibly
    # partial) or None; either is acceptable, but no exception escapes.
    prov = provider_for("/repo/src/broken.js")
    assert prov is not None
    result = prov.extract_tree("function ( { { {", "/repo/src/broken.js")
    assert result is None or result.scope_graph is not None


# -- per-language parity ----------------------------------------------------
#
# Every tree-sitter language reuses the one config-driven builder, so a single
# parametrized contract proves each: the class-like symbol + its method label,
# the self/this-receiver same-file edge, and the bare-name same-file edge. Each
# case is a (path, source, method_qual, self_edge, bare_edge) tuple; a ``None``
# self_edge means the language has no receiver-based self call (Go, C).

_GO = (
    "/repo/pkg/main.go",
    "package main\n" "func helper() int { return 1 }\n" "func run() int { return helper() }\n",
    None,  # Go: no self keyword → no receiver self-edge
    None,
    ("run", "helper"),
)

_RUST = (
    "/repo/src/main.rs",
    "struct S;\n"
    "impl S {\n"
    "    fn a(&self) -> i32 { self.b() }\n"
    "    fn b(&self) -> i32 { 1 }\n"
    "}\n"
    "fn helper() -> i32 { 1 }\n"
    "fn run() -> i32 { helper() }\n",
    "S.a",
    ("S.a", "S.b"),
    ("run", "helper"),
)

_JAVA = (
    "/repo/src/App.java",
    "class App {\n"
    "    void a() { this.b(); }\n"
    "    void b() {}\n"
    "    void c() { helper(); }\n"
    "    void helper() {}\n"
    "}\n",
    "App.a",
    ("App.a", "App.b"),
    ("App.c", "App.helper"),
)

_CSHARP = (
    "/repo/src/App.cs",
    "class App {\n"
    "    void A() { this.B(); }\n"
    "    void B() {}\n"
    "    void C() { Helper(); }\n"
    "    void Helper() {}\n"
    "}\n",
    "App.A",
    ("App.A", "App.B"),
    ("App.C", "App.Helper"),
)

_C = (
    "/repo/src/main.c",
    "int helper(void) { return 1; }\n" "int run(void) { return helper(); }\n",
    None,  # C: no methods / receiver
    None,
    ("run", "helper"),
)

_CPP = (
    "/repo/src/widget.cpp",
    "class C {\n"
    "    void a() { this->b(); }\n"
    "    void b() {}\n"
    "};\n"
    "int helper() { return 1; }\n"
    "int run() { return helper(); }\n",
    "C.a",
    ("C.a", "C.b"),
    ("run", "helper"),
)


@pytest.mark.parametrize(
    "path, source, method_qual, self_edge, bare_edge",
    [_GO, _RUST, _JAVA, _CSHARP, _C, _CPP],
    ids=["go", "rust", "java", "csharp", "c", "cpp"],
)
def test_language_parity(path, source, method_qual, self_edge, bare_edge):
    ex = _extract(source, path)
    pairs = {(e[0].qualified_name, e[1].qualified_name) for e in ex.scope_graph.call_edges()}
    quals = {s.qualified_name: s for s in ex.symbols}

    if method_qual is not None:
        assert quals[method_qual].kind == "method"
    if self_edge is not None:
        assert self_edge in pairs
    assert bare_edge in pairs


# Every language spells its return type differently (Go ``result`` tuple, Rust /
# TS ``return_type``, Java ``type``, C# ``returns``); the builder normalizes them
# all to the one ``-> ret`` form the outline already uses for Python. Each case:
# (path, source, symbol qual, expected exact signature incl. params + defaults +
# the normalized return). A second entry per language proves a void/unit return
# yields NO ``->`` suffix (not ``-> ()`` or ``-> void``-noise).
_RET_SIG = [
    (
        "/r/x.go",
        "package p\nfunc Foo(a int, b string) (int, error) { return 0, nil }\n",
        "Foo",
        "(a int, b string) -> (int, error)",
    ),
    ("/r/v.go", "package p\nfunc Bar(x int) { }\n", "Bar", "(x int)"),
    (
        "/r/x.rs",
        "fn foo(a: i32, b: String) -> Result<i32, String> { Ok(0) }\n",
        "foo",
        "(a: i32, b: String) -> Result<i32, String>",
    ),
    ("/r/v.rs", "fn noret(x: i32) { }\n", "noret", "(x: i32)"),
    (
        "/r/x.ts",
        "export function foo(a: number, b = 3): Promise<number> { return Promise.resolve(1); }\n",
        "foo",
        "(a: number, b = 3) -> Promise<number>",
    ),
    ("/r/v.ts", "function noret(x: number) { }\n", "noret", "(x: number)"),
    ("/r/X.java", "class C { public int foo(int a, String b) { return 0; } }\n", "C.foo", "(int a, String b) -> int"),
    ("/r/V.java", "class C { void bar(int a) {} }\n", "C.bar", "(int a) -> void"),
    ("/r/X.cs", "class C { public int Foo(int a, string b) { return 0; } }\n", "C.Foo", "(int a, string b) -> int"),
    ("/r/x.js", "function foo(a, b = 3) { return 1; }\n", "foo", "(a, b = 3)"),
]


@pytest.mark.parametrize(
    "path, source, qual, expected_sig",
    _RET_SIG,
    ids=[f"{c[0].rsplit('/', 1)[-1]}:{c[2]}" for c in _RET_SIG],
)
def test_signature_carries_params_and_return(path, source, qual, expected_sig):
    ex = _extract(source, path)
    sym = {s.qualified_name: s for s in ex.symbols}[qual]
    assert sym.signature == expected_sig
