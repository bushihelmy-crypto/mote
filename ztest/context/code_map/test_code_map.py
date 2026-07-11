"""Tests for the CodeMap facade — lazy refresh + touched-set neighborhood."""

from __future__ import annotations

import os

from mote.context.code_map import CodeMap


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


# -- Feature 1: intra-file call edges surface in the neighborhood --------------


def test_neighborhood_reports_intra_file_calls(tmp_path):
    path = _write(
        str(tmp_path),
        "c.py",
        "def helper():\n    pass\n\ndef run():\n    helper()\n",
    )
    cm = CodeMap()
    nb = cm.neighborhood([path])[0]
    pairs = {(c.caller, c.callee) for c in nb.calls}
    assert ("run", "helper") in pairs


def test_neighborhood_calls_empty_when_no_local_calls(tmp_path):
    # A call to an *imported* / undefined name is not an intra-file edge.
    path = _write(str(tmp_path), "c.py", "import os\n\ndef run():\n    os.getcwd()\n")
    cm = CodeMap()
    nb = cm.neighborhood([path])[0]
    assert nb.calls == []


# -- Feature 2 (Layer A): dangling internal import edges -----------------------


def test_dangling_import_to_untouched_repo_file(tmp_path):
    base = str(tmp_path)
    _write(base, "pkg/__init__.py", "")
    _write(base, "pkg/other.py", "def thing():\n    pass\n")
    # consumer imports an internal module that is NOT in the touched set.
    consumer = _write(base, "pkg/consumer.py", "import pkg.other\n")

    cm = CodeMap()
    nb = cm.neighborhood([consumer])[0]
    other_abs = os.path.abspath(os.path.join(base, "pkg/other.py"))
    assert other_abs in nb.imports_unread
    # Not a within-set edge (other.py was not touched).
    assert nb.imports == []


def test_within_set_import_not_also_dangling(tmp_path):
    base = str(tmp_path)
    _write(base, "pkg/__init__.py", "")
    helper = _write(base, "pkg/helper.py", "def util():\n    pass\n")
    consumer = _write(base, "pkg/consumer.py", "import pkg.helper\n")

    cm = CodeMap()
    nb = {n.path: n for n in cm.neighborhood([helper, consumer])}
    consumer_nb = nb[os.path.abspath(consumer)]
    helper_abs = os.path.abspath(helper)
    # helper is touched -> a within-set edge, and must NOT be double-reported as
    # a dangling edge.
    assert helper_abs in consumer_nb.imports
    assert helper_abs not in consumer_nb.imports_unread


def test_stdlib_import_is_not_dangling(tmp_path):
    base = str(tmp_path)
    consumer = _write(base, "consumer.py", "import os\nimport json\n")
    cm = CodeMap()
    nb = cm.neighborhood([consumer])[0]
    # stdlib maps to no repo file -> not surfaced as a dangling edge.
    assert nb.imports_unread == []


def test_bare_relative_import_not_treated_as_dangling(tmp_path):
    base = str(tmp_path)
    _write(base, "pkg/__init__.py", "")
    _write(base, "pkg/sibling.py", "def s():\n    pass\n")
    consumer = _write(base, "pkg/consumer.py", "from . import sibling\n")
    cm = CodeMap()
    nb = cm.neighborhood([consumer])[0]
    # ``from . import sibling`` names no module (only the package) -> it would
    # resolve only to the package __init__, an imprecise edge, so it's left
    # dotted-and-skipped rather than surfaced as noise.
    assert nb.imports_unread == []


def test_module_relative_import_surfaces_as_dangling(tmp_path):
    # RECALL FIX: ``from .other import thing`` names a sibling module; it resolves
    # to an absolute dotted name and, when that sibling is untouched, surfaces as
    # a dangling edge (previously relative imports were dropped entirely).
    base = str(tmp_path)
    _write(base, "pkg/__init__.py", "")
    other = _write(base, "pkg/other.py", "def thing():\n    pass\n")
    consumer = _write(base, "pkg/consumer.py", "from .other import thing\n")
    cm = CodeMap()
    nb = cm.neighborhood([consumer])[0]
    assert os.path.abspath(other) in nb.imports_unread


def test_relative_import_reverse_edge_within_touched_set(tmp_path):
    # RECALL FIX: a relative importer now appears in the target's reverse edge.
    base = str(tmp_path)
    _write(base, "pkg/__init__.py", "")
    helper = _write(base, "pkg/helper.py", "def util():\n    pass\n")
    consumer = _write(base, "pkg/consumer.py", "from .helper import util\n")
    cm = CodeMap()
    nb = {n.path: n for n in cm.neighborhood([helper, consumer])}
    consumer_abs = os.path.abspath(consumer)
    helper_abs = os.path.abspath(helper)
    # forward: consumer imports helper via a relative import.
    assert helper_abs in nb[consumer_abs].imports
    # reverse: helper is used by consumer — the edge relative imports used to miss.
    assert consumer_abs in nb[helper_abs].imported_by


def test_relative_importer_surfaces_in_whole_repo_used_by(tmp_path):
    # RECALL FIX for Layer C: an *untouched* file that imports the target via a
    # relative import is found by the whole-repo reverse-dep query. We drive the
    # repo index over the same store so its edges include the relative importer.
    base = str(tmp_path)
    _write(base, "pkg/__init__.py", "")
    helper = _write(base, "pkg/helper.py", "def util():\n    pass\n")
    faraway = _write(base, "pkg/faraway.py", "from .helper import util\n")

    cm = CodeMap()
    # Index the untouched relative importer into the store (as the RepoIndexer
    # would during a scan), then query whole-repo importers of the touched file.
    cm.ensure_fresh(faraway)
    nb = cm.neighborhood([helper], repo_importers=cm._store.importers_repo)[0]
    assert os.path.abspath(faraway) in nb.imported_by


def test_import_roots_climbs_out_of_package(tmp_path):
    base = str(tmp_path)
    _write(base, "pkg/__init__.py", "")
    _write(base, "pkg/sub/__init__.py", "")
    mod = _write(base, "pkg/sub/mod.py", "x = 1\n")
    roots = CodeMap._import_roots([os.path.abspath(mod)])
    # mod sits in the pkg.sub package, so the anchor is the repo dir above pkg.
    assert os.path.abspath(base) in roots


def test_import_roots_bare_script_anchors_at_its_dir(tmp_path):
    base = str(tmp_path)
    script = _write(base, "solo.py", "x = 1\n")  # no __init__.py alongside
    roots = CodeMap._import_roots([os.path.abspath(script)])
    assert os.path.abspath(base) in roots


# -- cache bounding ------------------------------------------------------------


def test_evict_stale_drops_prior_version_rows():
    # A version-keyed cache is only read at the current version; prior-version
    # rows for the same identity are evicted on write to keep it O(live symbols).
    cache = {
        ("a.py", "hash1"): ["x"],
        ("a.py", "hash2"): ["y"],  # current
        ("b.py", "hash1"): ["z"],  # different identity — untouched
    }
    CodeMap._evict_stale(cache, identity=("a.py",), keep=("a.py", "hash2"))
    assert cache == {("a.py", "hash2"): ["y"], ("b.py", "hash1"): ["z"]}


def _run(coro):
    import asyncio

    return asyncio.run(coro)


class _FakeRefQuery:
    def __init__(self, uri):
        self._uri = uri
        self.calls = 0

    async def references(self, path, line, character):
        self.calls += 1
        return [{"uri": self._uri}]


def test_precise_callers_cache_bounded_across_edits(tmp_path):
    from pathlib import Path

    base = str(tmp_path)
    api = _write(base, "api.py", "def foo(x):\n    pass\n")
    caller = _write(base, "caller.py", "import api\n")
    cm = CodeMap()
    cm.ensure_all_fresh([os.path.abspath(api)])
    lsp = _FakeRefQuery(Path(os.path.abspath(caller)).as_uri())

    # Re-parse + query across three distinct file versions.
    for extra in range(3):
        body = "def foo(x" + ", y" * extra + "):\n    pass\n"
        _write(base, "api.py", body)
        os.utime(api, ns=(0, os.stat(api).st_mtime_ns + (extra + 1) * 1_000_000_000))
        cm.ensure_all_fresh([os.path.abspath(api)])
        nb = cm.neighborhood([os.path.abspath(api)])[0]
        _run(cm.precise_callers(api, nb.symbols, lsp))

    # Only the current version's row survives for foo — not one per edit.
    keys = [k for k in cm._refs_cache if k[0] == os.path.abspath(api)]
    assert len(keys) == 1
