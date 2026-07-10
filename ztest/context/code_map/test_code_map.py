"""Tests for the CodeMap facade — lazy refresh + touched-set neighborhood."""

from __future__ import annotations

import os

from metagpt.context.code_map import CodeMap


def _write(base, relpath: str, source: str) -> str:
    full = os.path.join(base, relpath)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(source)
    return full


def test_ensure_fresh_populates_then_skips(tmp_path, monkeypatch):
    path = _write(str(tmp_path), "m.py", "def f():\n    pass\n")
    cm = CodeMap()

    cm.ensure_fresh(path)
    assert [s.name for s in cm._store.symbols_in(os.path.abspath(path))] == ["f"]

    # Second call with no change must not re-extract (spy on extractor).
    calls = {"n": 0}
    orig = cm._extractor.extract

    def _spy(p):
        calls["n"] += 1
        return orig(p)

    monkeypatch.setattr(cm._extractor, "extract", _spy)
    cm.ensure_fresh(path)
    assert calls["n"] == 0


def test_neighborhood_reports_defined_symbols(tmp_path):
    path = _write(str(tmp_path), "defs.py", "class C:\n    def m(self):\n        pass\n")
    cm = CodeMap()
    nb = cm.neighborhood([path])
    assert len(nb) == 1
    quals = {s.qualified_name for s in nb[0].symbols}
    assert "C" in quals and "C.m" in quals


def test_neighborhood_forward_import_within_touched_set(tmp_path):
    base = str(tmp_path)
    # consumer imports the `helper` module by bare name.
    helper = _write(base, "helper.py", "def util():\n    pass\n")
    consumer = _write(base, "consumer.py", "import helper\n")

    cm = CodeMap()
    nb = {n.path: n for n in cm.neighborhood([helper, consumer])}

    consumer_abs = os.path.abspath(consumer)
    helper_abs = os.path.abspath(helper)
    # consumer imports helper -> forward edge.
    assert helper_abs in nb[consumer_abs].imports
    # helper is imported by consumer -> reverse edge.
    assert consumer_abs in nb[helper_abs].imported_by


def test_neighborhood_from_import_resolves(tmp_path):
    base = str(tmp_path)
    helper = _write(base, "helper.py", "def util():\n    pass\n")
    consumer = _write(base, "consumer.py", "from helper import util\n")

    cm = CodeMap()
    nb = {n.path: n for n in cm.neighborhood([helper, consumer])}
    assert os.path.abspath(helper) in nb[os.path.abspath(consumer)].imports


def test_neighborhood_ignores_imports_outside_touched_set(tmp_path):
    base = str(tmp_path)
    consumer = _write(base, "consumer.py", "import os\nimport json\n")
    cm = CodeMap()
    nb = cm.neighborhood([consumer])
    # os / json are not in the touched set -> no forward edges.
    assert nb[0].imports == []
    assert nb[0].imported_by == []


def test_neighborhood_lists_non_python_with_empty_structure(tmp_path):
    base = str(tmp_path)
    txt = _write(base, "notes.txt", "hello\n")
    cm = CodeMap()
    nb = cm.neighborhood([txt])
    assert len(nb) == 1
    assert nb[0].symbols == []
    assert nb[0].imports == []


def test_neighborhood_reflects_edit_on_reparse(tmp_path):
    path = _write(str(tmp_path), "e.py", "def a():\n    pass\n")
    cm = CodeMap()
    nb = cm.neighborhood([path])
    assert {s.name for s in nb[0].symbols} == {"a"}

    # Edit the file; mtime bumps; neighborhood must reflect the new symbol set.
    _write(str(tmp_path), "e.py", "def b():\n    pass\n")
    # Force a distinctly later mtime so the freshness check fires even when the
    # rewrite lands within the same clock tick as the first parse.
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    nb2 = cm.neighborhood([path])
    assert {s.name for s in nb2[0].symbols} == {"b"}


def test_module_candidates_progressive_suffixes():
    cands = CodeMap._module_candidates("/root/a/b/c.py")
    assert "c" in cands
    assert "b.c" in cands
    assert "a.b.c" in cands


def test_module_candidates_package_init_uses_dir_name():
    cands = CodeMap._module_candidates("/root/pkg/__init__.py")
    assert "pkg" in cands
    assert "__init__" not in cands


def test_module_candidates_non_python_empty():
    assert CodeMap._module_candidates("/root/readme.md") == set()
