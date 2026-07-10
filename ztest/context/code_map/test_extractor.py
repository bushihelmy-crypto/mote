"""Tests for CodeMapExtractor — ast-derived symbols, imports, and call edges."""

from __future__ import annotations

import os

from metagpt.context.code_map.extractor import CodeMapExtractor


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
    src = "from . import sibling\nfrom ..pkg import other\n"
    path = _write(tmp_path, "rel.py", src)
    extract = CodeMapExtractor().extract(path)
    assert "." in extract.imports  # from . import sibling
    assert "..pkg" in extract.imports


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
