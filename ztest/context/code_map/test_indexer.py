"""Tests for RepoIndexer — whole-repo persistent reverse-dep index (Layer C)."""

from __future__ import annotations

import os

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
