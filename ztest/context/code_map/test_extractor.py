"""Tests for CodeMapExtractor — ast-derived symbols, imports, and call edges."""

from __future__ import annotations

import os

from mote.context.code_map.extractor import CodeMapExtractor


def _write(tmp_path, name: str, source: str) -> str:
    p = tmp_path / name
    p.write_text(source, encoding="utf-8")
    return str(p)


def test_extracts_functions_classes_and_methods(tmp_path):
    src = """
def top_level():
    pass

class Foo:
    def method(self, x):
        return x

    async def amethod(self):
        pass
"""
    path = _write(tmp_path, "m.py", src)
    ex = CodeMapExtractor()
    extract = ex.extract(path)

    by_qual = {s.qualified_name: s for s in extract.symbols}
    assert by_qual["top_level"].kind == "function"
    assert by_qual["Foo"].kind == "class"
    assert by_qual["Foo.method"].kind == "method"
    assert by_qual["Foo.amethod"].kind == "method"
    # start_line is 1-based and points at the def/class line.
    assert by_qual["top_level"].start_line == 2


def test_signature_includes_params_and_return(tmp_path):
    src = "def f(a: int, b: str = 'x') -> bool:\n    return True\n"
    path = _write(tmp_path, "sig.py", src)
    extract = CodeMapExtractor().extract(path)
    sym = extract.symbols[0]
    assert sym.signature.startswith("(")
    assert "a: int" in sym.signature
    assert "-> bool" in sym.signature


def test_class_has_empty_signature(tmp_path):
    path = _write(tmp_path, "c.py", "class C:\n    pass\n")
    extract = CodeMapExtractor().extract(path)
    assert extract.symbols[0].kind == "class"
    assert extract.symbols[0].signature == ""


def test_collects_imports_absolute_and_from(tmp_path):
    src = "import os\nimport a.b.c\nfrom pkg.mod import thing\n"
    path = _write(tmp_path, "imp.py", src)
    extract = CodeMapExtractor().extract(path)
    assert "os" in extract.imports
    assert "a.b.c" in extract.imports
    assert "pkg.mod" in extract.imports


def test_relative_import_prefixed_with_dots(tmp_path):
    # A bare (non-package) file has no package anchor, so a relative import can't
    # be resolved to an absolute name and keeps its dotted spelling.
    src = "from . import sibling\nfrom ..pkg import other\n"
    path = _write(tmp_path, "rel.py", src)
    extract = CodeMapExtractor().extract(path)
    assert "." in extract.imports  # from . import sibling — module-less, stays dotted
    assert "..pkg" in extract.imports  # unanchorable (bare file) -> dotted fallback


def _pkg_file(tmp_path, relpath: str, source: str) -> str:
    """Write a file inside a package, creating __init__.py up the chain."""
    full = os.path.join(str(tmp_path), relpath)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    d = os.path.dirname(full)
    base = os.path.abspath(str(tmp_path))
    while os.path.abspath(d) != base and len(os.path.abspath(d)) > len(base):
        init = os.path.join(d, "__init__.py")
        if not os.path.exists(init):
            open(init, "w").close()
        d = os.path.dirname(d)
    with open(full, "w", encoding="utf-8") as f:
        f.write(source)
    return full


def test_relative_import_resolved_to_absolute_in_package(tmp_path):
    # A file inside package a.b importing `from .other import thing` resolves to
    # the absolute dotted name a.b.other — recall for reverse-dep/dangling.
    path = _pkg_file(tmp_path, "a/b/consumer.py", "from .other import thing\n")
    extract = CodeMapExtractor().extract(path)
    assert "a.b.other" in extract.imports
    # No leading-dot spelling survives once resolved.
    assert not any(m.startswith(".") for m in extract.imports)


def test_relative_import_climbs_packages(tmp_path):
    # `from ..sib import x` from a.b.consumer climbs to package a, descends sib.
    path = _pkg_file(tmp_path, "a/b/consumer.py", "from ..sib import x\n")
    extract = CodeMapExtractor().extract(path)
    assert "a.sib" in extract.imports


def test_relative_import_refs_resolved_to_absolute(tmp_path):
    # The parallel import_refs carry the same resolved absolute module.
    path = _pkg_file(tmp_path, "a/b/consumer.py", "from .other import thing\n")
    extract = CodeMapExtractor().extract(path)
    by_name = {r.name: r for r in extract.import_refs}
    assert by_name["thing"].module == "a.b.other"


def test_relative_import_overshoot_keeps_dotted(tmp_path):
    # Climbing past the package root is unanchorable -> dotted fallback (no crash).
    path = _pkg_file(tmp_path, "a/consumer.py", "from ...toofar import x\n")
    extract = CodeMapExtractor().extract(path)
    assert "...toofar" in extract.imports


def test_imports_deduped_order_preserved(tmp_path):
    src = "import os\nimport os\nimport sys\n"
    path = _write(tmp_path, "dup.py", src)
    extract = CodeMapExtractor().extract(path)
    assert extract.imports == ["os", "sys"]


def test_same_file_call_edge_recorded(tmp_path):
    src = """
def helper():
    pass

def caller():
    helper()
"""
    path = _write(tmp_path, "call.py", src)
    extract = CodeMapExtractor().extract(path)
    edges = [(c.caller, c.callee) for c in extract.calls]
    assert ("caller", "helper") in edges


def test_call_to_undefined_name_not_recorded(tmp_path):
    # print / external names are not same-file definitions -> no edge.
    src = "def f():\n    print('hi')\n"
    path = _write(tmp_path, "ext.py", src)
    extract = CodeMapExtractor().extract(path)
    assert extract.calls == []


def test_method_call_attributed_to_qualified_caller(tmp_path):
    src = """
def target():
    pass

class C:
    def m(self):
        target()
"""
    path = _write(tmp_path, "mcall.py", src)
    extract = CodeMapExtractor().extract(path)
    edges = [(c.caller, c.callee) for c in extract.calls]
    assert ("C.m", "target") in edges


def test_self_method_call_recorded(tmp_path):
    # self.helper() targets a same-class method defined here -> a real edge.
    src = """
class C:
    def helper(self):
        pass

    def run(self):
        self.helper()
"""
    path = _write(tmp_path, "selfcall.py", src)
    extract = CodeMapExtractor().extract(path)
    edges = [(c.caller, c.callee) for c in extract.calls]
    assert ("C.run", "helper") in edges


def test_attribute_call_on_foreign_receiver_not_recorded(tmp_path):
    # x.helper() names an object we cannot type from the AST; matching the bare
    # name "helper" against a same-named local def would be a false positive.
    src = """
def helper():
    pass

def run(x):
    x.helper()
"""
    path = _write(tmp_path, "attrcall.py", src)
    extract = CodeMapExtractor().extract(path)
    edges = [(c.caller, c.callee) for c in extract.calls]
    assert ("run", "helper") not in edges
    assert extract.calls == []


def test_syntax_error_yields_empty_extract(tmp_path):
    path = _write(tmp_path, "bad.py", "def broken(:\n")
    extract = CodeMapExtractor().extract(path)
    assert extract.symbols == []
    assert extract.imports == []
    assert extract.calls == []
    # path is still recorded (absolute).
    assert extract.path == os.path.abspath(path)


def test_unreadable_path_yields_empty_extract(tmp_path):
    missing = str(tmp_path / "nope.py")
    extract = CodeMapExtractor().extract(missing)
    assert extract.symbols == []
    assert extract.path == os.path.abspath(missing)


def test_needs_refresh_true_before_first_parse(tmp_path):
    path = _write(tmp_path, "fresh.py", "x = 1\n")
    ex = CodeMapExtractor()
    assert ex.needs_refresh(path) is True


def test_needs_refresh_false_after_parse_until_changed(tmp_path):
    path = _write(tmp_path, "fresh2.py", "x = 1\n")
    ex = CodeMapExtractor()
    ex.extract(path)
    assert ex.needs_refresh(path) is False

    # Touch content -> mtime changes -> refresh needed again.
    os.utime(path, ns=(0, 0))
    assert ex.needs_refresh(path) is True


def test_needs_refresh_false_for_missing_file(tmp_path):
    ex = CodeMapExtractor()
    assert ex.needs_refresh(str(tmp_path / "gone.py")) is False


# -- Layer B: import_refs (binding + position) --------------------------------


def test_import_refs_from_import_carries_name_and_position(tmp_path):
    src = "from pkg.other import thing\n"
    path = _write(tmp_path, "ir.py", src)
    extract = CodeMapExtractor().extract(path)
    by_name = {r.name: r for r in extract.import_refs}
    assert "thing" in by_name
    ref = by_name["thing"]
    assert ref.module == "pkg.other"
    assert ref.line == 1  # 1-based
    assert ref.col >= 0  # 0-based character offset


def test_import_refs_plain_import_binds_alias(tmp_path):
    src = "import a.b.c as abc\nimport os\n"
    path = _write(tmp_path, "ir2.py", src)
    extract = CodeMapExtractor().extract(path)
    by_name = {r.name: r for r in extract.import_refs}
    # aliased import binds the asname; the module is the full dotted target.
    assert by_name["abc"].module == "a.b.c"
    # plain import binds the module name itself.
    assert by_name["os"].module == "os"


def test_import_refs_multiple_names_one_from(tmp_path):
    src = "from pkg import a, b, c\n"
    path = _write(tmp_path, "ir3.py", src)
    extract = CodeMapExtractor().extract(path)
    names = {r.name for r in extract.import_refs}
    assert names == {"a", "b", "c"}
    assert all(r.module == "pkg" for r in extract.import_refs)


# -- Layer C: content_hash ----------------------------------------------------


def test_content_hash_stable_across_reparse(tmp_path):
    path = _write(tmp_path, "h.py", "x = 1\n")
    ex = CodeMapExtractor()
    h1 = ex.extract(path).content_hash
    h2 = CodeMapExtractor().extract(path).content_hash
    assert h1 and h1 == h2  # deterministic, non-empty


def test_content_hash_changes_on_edit(tmp_path):
    path = _write(tmp_path, "h2.py", "x = 1\n")
    h1 = CodeMapExtractor().extract(path).content_hash
    _write(tmp_path, "h2.py", "x = 2\n")
    h2 = CodeMapExtractor().extract(path).content_hash
    assert h1 != h2


def test_content_hash_present_even_on_syntax_error(tmp_path):
    path = _write(tmp_path, "hbad.py", "def broken(:\n")
    extract = CodeMapExtractor().extract(path)
    # A broken file still carries a stable content hash so the store's staleness
    # diff sees it as parsed-at-this-version (no perpetual re-parse).
    assert extract.content_hash != ""


# -- P1: docstring summaries --------------------------------------------------


def test_module_summary_first_line(tmp_path):
    src = '"""One-line module purpose.\n\nMore detail below.\n"""\nx = 1\n'
    path = _write(tmp_path, "docmod.py", src)
    extract = CodeMapExtractor().extract(path)
    assert extract.module_summary == "One-line module purpose."


def test_module_summary_empty_when_undocumented(tmp_path):
    path = _write(tmp_path, "nodoc.py", "x = 1\n")
    extract = CodeMapExtractor().extract(path)
    assert extract.module_summary == ""


def test_symbol_summary_from_docstring(tmp_path):
    src = 'def f():\n    """Does a thing."""\n    return 1\n'
    path = _write(tmp_path, "fdoc.py", src)
    sym = CodeMapExtractor().extract(path).symbols[0]
    assert sym.summary == "Does a thing."


def test_class_summary_from_docstring(tmp_path):
    src = 'class C:\n    """Holds state."""\n    pass\n'
    path = _write(tmp_path, "cdoc.py", src)
    sym = CodeMapExtractor().extract(path).symbols[0]
    assert sym.kind == "class"
    assert sym.summary == "Holds state."


def test_symbol_summary_empty_when_undocumented(tmp_path):
    src = "def f():\n    return 1\n"
    path = _write(tmp_path, "fnodoc.py", src)
    sym = CodeMapExtractor().extract(path).symbols[0]
    assert sym.summary == ""


def test_summary_first_nonblank_line_used(tmp_path):
    # A docstring whose content starts after a blank line: the first *meaningful*
    # line is taken (ast.get_docstring cleans leading indentation).
    src = 'def f():\n    """\n    Actual summary here.\n    """\n    pass\n'
    path = _write(tmp_path, "blankdoc.py", src)
    sym = CodeMapExtractor().extract(path).symbols[0]
    assert sym.summary == "Actual summary here."


def test_summary_whitespace_collapsed(tmp_path):
    src = 'def f():\n    """Has    inner   spaces."""\n    pass\n'
    path = _write(tmp_path, "wsdoc.py", src)
    sym = CodeMapExtractor().extract(path).symbols[0]
    assert sym.summary == "Has inner spaces."


def test_summary_truncated_when_long(tmp_path):
    long_line = "x " * 100  # far past the cap
    src = f'def f():\n    """{long_line}"""\n    pass\n'
    path = _write(tmp_path, "longdoc.py", src)
    sym = CodeMapExtractor().extract(path).symbols[0]
    assert len(sym.summary) <= CodeMapExtractor._SUMMARY_MAX_CHARS
    assert sym.summary.endswith("…")
