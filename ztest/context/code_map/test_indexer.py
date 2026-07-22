"""Tests for RepoIndexer — whole-repo persistent reverse-dep index (Layer C)."""

from __future__ import annotations

import os

import pytest

from mote.context.code_map.indexer import RepoIndexer


def _write(tmp_path, rel: str, source: str) -> str:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(source, encoding="utf-8")
    return str(p)


def _indexer(tmp_path):
    # Persist to a DB inside the tmp tree so the ~/.mote path isn't touched.
    db = str(tmp_path / "codemap.db")
    return RepoIndexer(str(tmp_path), store_path=db)


def test_constructor_does_not_create_database_or_parent(tmp_path):
    database = tmp_path / "nested" / "codemap.db"
    indexer = RepoIndexer(str(tmp_path), store_path=str(database))
    assert indexer._map is None
    assert not database.parent.exists()

    indexer.prepare()
    assert database.exists()
    indexer.close()


def test_scan_all_indexes_every_py(tmp_path):
    _write(tmp_path, "a.py", "import b\n")
    _write(tmp_path, "b.py", "x = 1\n")
    idx = _indexer(tmp_path)
    try:
        idx.scan_all()
        indexed = set(idx._map._store.all_indexed_paths())
        assert os.path.join(str(tmp_path), "a.py") in indexed
        assert os.path.join(str(tmp_path), "b.py") in indexed
    finally:
        idx.close()


def test_scan_all_skips_prune_dirs(tmp_path):
    _write(tmp_path, "keep.py", "x = 1\n")
    _write(tmp_path, ".venv/skip.py", "y = 2\n")
    _write(tmp_path, "__pycache__/cached.py", "z = 3\n")
    idx = _indexer(tmp_path)
    try:
        idx.scan_all()
        indexed = set(idx._map._store.all_indexed_paths())
        assert os.path.join(str(tmp_path), "keep.py") in indexed
        assert not any(".venv" in p or "__pycache__" in p for p in indexed)
    finally:
        idx.close()


def test_importers_returns_whole_repo(tmp_path):
    # a.py and c.py both import module "b"; the query should surface both.
    _write(tmp_path, "a.py", "import b\n")
    _write(tmp_path, "c.py", "import b\n")
    _write(tmp_path, "b.py", "x = 1\n")
    idx = _indexer(tmp_path)
    try:
        idx.scan_all()
        importers = set(idx.importers(["b"]))
        assert importers == {
            os.path.join(str(tmp_path), "a.py"),
            os.path.join(str(tmp_path), "c.py"),
        }
    finally:
        idx.close()


def test_refresh_updates_only_changed_file(tmp_path):
    _write(tmp_path, "a.py", "import b\n")
    _write(tmp_path, "b.py", "x = 1\n")
    idx = _indexer(tmp_path)
    try:
        idx.scan_all()
        # a.py stops importing b, starts importing os.
        apath = _write(tmp_path, "a.py", "import os\n")
        idx.refresh([apath])
        assert idx.importers(["b"]) == []
        assert set(idx.importers(["os"])) == {apath}
    finally:
        idx.close()


def test_refresh_deletes_vanished_file(tmp_path):
    apath = _write(tmp_path, "a.py", "import b\n")
    _write(tmp_path, "b.py", "x = 1\n")
    idx = _indexer(tmp_path)
    try:
        idx.scan_all()
        assert set(idx.importers(["b"])) == {apath}
        os.remove(apath)
        idx.refresh([apath])
        assert idx.importers(["b"]) == []
    finally:
        idx.close()


def test_refresh_ignores_non_py(tmp_path):
    txt = _write(tmp_path, "note.txt", "not python\n")
    idx = _indexer(tmp_path)
    idx.prepare()
    try:
        idx.refresh([txt])  # no crash, nothing indexed
        assert idx._map._store.all_indexed_paths() == []
    finally:
        idx.close()


def test_scan_all_warm_store_skips_unchanged(tmp_path):
    _write(tmp_path, "a.py", "import b\n")
    _write(tmp_path, "b.py", "x = 1\n")
    idx = _indexer(tmp_path)
    try:
        idx.scan_all()
        # Spy on the extractor: a warm re-scan of unchanged files reparses nothing.
        calls: list = []
        original = idx._hasher.extract

        def spy(path):
            calls.append(path)
            return original(path)

        idx._hasher.extract = spy  # type: ignore[assignment]
        idx.scan_all()
        assert calls == []  # nothing stale -> no re-parse
    finally:
        idx.close()


def test_symbols_in_returns_whole_repo_symbols(tmp_path):
    other = _write(tmp_path, "pkg/other.py", "def thing():\n    pass\n\nclass Widget:\n    pass\n")
    _write(tmp_path, "pkg/__init__.py", "")
    idx = _indexer(tmp_path)
    try:
        idx.scan_all()
        names = {s.name for s in idx.symbols_in(os.path.abspath(other))}
        assert names == {"thing", "Widget"}
    finally:
        idx.close()


def test_symbols_in_best_effort_on_store_error(tmp_path):
    idx = _indexer(tmp_path)
    idx.prepare()
    try:

        class _Boom:
            def symbols_in(self, path):
                raise RuntimeError("boom")

            def close(self):
                pass

        idx._map._store = _Boom()  # type: ignore[assignment]
        assert idx.symbols_in("whatever.py") == []
    finally:
        idx.close()


def test_module_summary_of_returns_docstring_first_line(tmp_path):
    mod = _write(tmp_path, "purposeful.py", '"""What this module is for."""\ndef f():\n    pass\n')
    idx = _indexer(tmp_path)
    try:
        idx.scan_all()
        assert idx.module_summary_of(os.path.abspath(mod)) == "What this module is for."
    finally:
        idx.close()


def test_module_summary_of_best_effort_on_store_error(tmp_path):
    idx = _indexer(tmp_path)
    idx.prepare()
    try:

        class _Boom:
            def module_summary_of(self, path):
                raise RuntimeError("boom")

            def close(self):
                pass

        idx._map._store = _Boom()  # type: ignore[assignment]
        assert idx.module_summary_of("whatever.py") == ""
    finally:
        idx.close()


# -- whole-repo navigation (Decision C) ---------------------------------------


def test_references_to_finds_cross_file_and_intra_file(tmp_path):
    _write(tmp_path, "pkg/__init__.py", "")
    other = _write(tmp_path, "pkg/other.py", "def thing():\n    pass\n\ndef run():\n    thing()\n")
    consumer = _write(tmp_path, "pkg/consumer.py", "from pkg.other import thing\n")
    idx = _indexer(tmp_path)
    try:
        idx.scan_all()
        refs = idx.references_to(os.path.abspath(other), "thing")
        paths = {p for p, _ in refs}
        assert os.path.abspath(other) in paths  # intra-file run() -> thing()
        assert os.path.abspath(consumer) in paths  # cross-file import site
    finally:
        idx.close()


def test_references_to_best_effort_on_store_error(tmp_path):
    idx = _indexer(tmp_path)
    try:

        class _Boom:
            def references_to(self, path, symbol):
                raise RuntimeError("boom")

            def close(self):
                pass

        idx._map = _Boom()  # type: ignore[assignment]
        assert idx.references_to("whatever.py", "thing") == []
    finally:
        pass


def test_definition_of_maps_module_to_symbol(tmp_path):
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/other.py", "def thing():\n    pass\n\nclass Widget:\n    pass\n")
    idx = _indexer(tmp_path)
    try:
        idx.scan_all()
        d = idx.definition_of("thing", "pkg.other")
        assert d is not None and d.kind == "function" and d.name == "thing"
        w = idx.definition_of("Widget", "pkg.other")
        assert w is not None and w.kind == "class"
    finally:
        idx.close()


def test_definition_of_none_for_unknown_module_or_name(tmp_path):
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/other.py", "def thing():\n    pass\n")
    idx = _indexer(tmp_path)
    try:
        idx.scan_all()
        assert idx.definition_of("thing", "pkg.nowhere") is None  # module maps to no file
        assert idx.definition_of("ghost", "pkg.other") is None  # name not defined
    finally:
        idx.close()


def test_scan_all_reparses_edited_file(tmp_path):
    _write(tmp_path, "a.py", "import b\n")
    idx = _indexer(tmp_path)
    try:
        idx.scan_all()
        apath = _write(tmp_path, "a.py", "import c\n")
        calls: list = []
        original = idx._hasher.extract

        def spy(path):
            calls.append(path)
            return original(path)

        idx._hasher.extract = spy  # type: ignore[assignment]
        idx.scan_all()
        assert apath in calls  # the edited file was re-parsed
    finally:
        idx.close()


# -- multi-language walk (Step 8) --------------------------------------------


def test_scan_all_indexes_tree_sitter_languages(tmp_path):
    pytest.importorskip("tree_sitter_language_pack")
    _write(tmp_path, "app.ts", 'import {x} from "./util";\n')
    _write(tmp_path, "util.ts", "export const x = 1;\n")
    _write(tmp_path, "main.go", "package main\n")
    idx = _indexer(tmp_path)
    try:
        idx.scan_all()
        indexed = set(idx._map._store.all_indexed_paths())
        assert os.path.join(str(tmp_path), "app.ts") in indexed
        assert os.path.join(str(tmp_path), "main.go") in indexed
    finally:
        idx.close()


def test_scan_all_prunes_new_build_dirs(tmp_path):
    pytest.importorskip("tree_sitter_language_pack")
    _write(tmp_path, "keep.go", "package main\n")
    _write(tmp_path, "node_modules/dep.js", "module.exports = 1;\n")
    _write(tmp_path, "target/gen.rs", "fn f() {}\n")
    _write(tmp_path, "build/out.cpp", "int f() { return 0; }\n")
    idx = _indexer(tmp_path)
    try:
        idx.scan_all()
        indexed = set(idx._map._store.all_indexed_paths())
        assert os.path.join(str(tmp_path), "keep.go") in indexed
        # Check path COMPONENTS relative to tmp_path — a substring probe would
        # false-match the tmp dir's own name (…_new_build_dirs contains "build").
        pruned = {"node_modules", "target", "build"}
        assert not any(pruned & set(os.path.relpath(p, str(tmp_path)).split(os.sep)) for p in indexed)
    finally:
        idx.close()


def test_walk_degrades_to_python_only_without_tree_sitter(tmp_path, monkeypatch):
    # With tree-sitter unavailable the registry is Python-only, so the walk filter
    # is exactly {".py"} — a .ts/.go file is ignored (identical to the old walk).
    import mote.context.code_map.indexer as indexer_mod

    monkeypatch.setattr(indexer_mod, "registered_extensions", lambda: {".py"})
    _write(tmp_path, "keep.py", "x = 1\n")
    _write(tmp_path, "skip.ts", "const x = 1;\n")
    idx = _indexer(tmp_path)
    try:
        idx.scan_all()
        indexed = set(idx._map._store.all_indexed_paths())
        assert os.path.join(str(tmp_path), "keep.py") in indexed
        assert not any(p.endswith(".ts") for p in indexed)
    finally:
        idx.close()
