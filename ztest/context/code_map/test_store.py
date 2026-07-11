"""Tests for CodeMapStore — SQLite nodes/edges persistence + neighborhood reads."""

from __future__ import annotations

from mote.context.code_map.extractor import CallEdge, FileExtract, Symbol
from mote.context.code_map.store import CodeMapStore


def _extract(path: str, *, symbols=None, imports=None, calls=None, content_hash="") -> FileExtract:
    return FileExtract(
        path=path,
        symbols=symbols or [],
        imports=imports or [],
        calls=calls or [],
        content_hash=content_hash,
    )


def test_symbols_roundtrip_ordered_by_line():
    store = CodeMapStore()
    ex = _extract(
        "/a.py",
        symbols=[
            Symbol(name="b", qualified_name="b", kind="function", start_line=10, signature="()"),
            Symbol(name="a", qualified_name="a", kind="function", start_line=2, signature="(x)"),
        ],
    )
    store.upsert_file(ex)
    got = store.symbols_in("/a.py")
    assert [s.qualified_name for s in got] == ["a", "b"]  # ordered by start_line
    assert got[0].signature == "(x)"


def test_imports_roundtrip_order_preserved():
    store = CodeMapStore()
    store.upsert_file(_extract("/a.py", imports=["os", "sys", "a.b"]))
    assert store.imports_of("/a.py") == ["os", "sys", "a.b"]


def test_calls_roundtrip_ordered_by_line():
    store = CodeMapStore()
    store.upsert_file(
        _extract(
            "/a.py",
            calls=[
                CallEdge(caller="f", callee="helper", line=5),
                CallEdge(caller="g", callee="helper", line=2),
            ],
        )
    )
    got = store.calls_in("/a.py")
    assert [(c.caller, c.line) for c in got] == [("g", 2), ("f", 5)]


def test_upsert_is_idempotent_replaces_prior_rows():
    store = CodeMapStore()
    store.upsert_file(
        _extract(
            "/a.py",
            symbols=[Symbol(name="old", qualified_name="old", kind="function", start_line=1)],
            imports=["stale"],
        )
    )
    # Re-parse with different content -> old rows must be gone.
    store.upsert_file(
        _extract(
            "/a.py",
            symbols=[Symbol(name="new", qualified_name="new", kind="function", start_line=1)],
            imports=["fresh"],
        )
    )
    assert [s.name for s in store.symbols_in("/a.py")] == ["new"]
    assert store.imports_of("/a.py") == ["fresh"]


def test_empty_file_yields_nothing():
    store = CodeMapStore()
    store.upsert_file(_extract("/empty.py"))
    assert store.symbols_in("/empty.py") == []
    assert store.imports_of("/empty.py") == []
    assert store.calls_in("/empty.py") == []


def test_unknown_file_reads_empty():
    store = CodeMapStore()
    assert store.symbols_in("/never.py") == []
    assert store.imports_of("/never.py") == []
    assert store.calls_in("/never.py") == []


def test_importers_within_matches_target_and_scope():
    store = CodeMapStore()
    store.upsert_file(_extract("/consumer.py", imports=["mod"]))
    store.upsert_file(_extract("/other.py", imports=["unrelated"]))

    # consumer imports "mod"; only it should come back, and only if in scope.
    got = store.importers_within(["mod"], ["/consumer.py", "/other.py"])
    assert got == ["/consumer.py"]


def test_importers_within_empty_targets_or_scope():
    store = CodeMapStore()
    store.upsert_file(_extract("/consumer.py", imports=["mod"]))
    assert store.importers_within([], ["/consumer.py"]) == []
    assert store.importers_within(["mod"], []) == []


def test_importers_within_respects_scope_exclusion():
    store = CodeMapStore()
    store.upsert_file(_extract("/consumer.py", imports=["mod"]))
    # consumer imports mod but is NOT in scope -> not reported.
    assert store.importers_within(["mod"], ["/somethingelse.py"]) == []


# -- Layer C: whole-repo reverse deps + files-table staleness -----------------


def test_importers_repo_ignores_scope_returns_out_of_scope():
    store = CodeMapStore()
    store.upsert_file(_extract("/consumer.py", imports=["mod"]))
    store.upsert_file(_extract("/faraway.py", imports=["mod"]))
    store.upsert_file(_extract("/other.py", imports=["unrelated"]))

    # The locality-drop assertion: every importer of "mod" comes back, with no
    # scope filter — including files the agent never touched.
    got = set(store.importers_repo(["mod"]))
    assert got == {"/consumer.py", "/faraway.py"}


def test_importers_repo_empty_targets():
    store = CodeMapStore()
    store.upsert_file(_extract("/consumer.py", imports=["mod"]))
    assert store.importers_repo([]) == []


def test_files_table_roundtrip_hash_and_indexed_paths():
    store = CodeMapStore()
    store.upsert_file(_extract("/a.py", imports=["os"], content_hash="h_a"))
    store.upsert_file(_extract("/b.py", imports=["sys"], content_hash="h_b"))
    assert set(store.all_indexed_paths()) == {"/a.py", "/b.py"}
    assert store.indexed_hashes() == {"/a.py": "h_a", "/b.py": "h_b"}


def test_upsert_replaces_files_hash():
    store = CodeMapStore()
    store.upsert_file(_extract("/a.py", content_hash="old"))
    store.upsert_file(_extract("/a.py", content_hash="new"))
    assert store.indexed_hashes() == {"/a.py": "new"}


def test_delete_file_removes_all_rows():
    store = CodeMapStore()
    store.upsert_file(
        _extract(
            "/a.py",
            symbols=[Symbol(name="f", qualified_name="f", kind="function", start_line=1)],
            imports=["os"],
            calls=[CallEdge(caller="f", callee="g", line=2)],
            content_hash="h",
        )
    )
    store.delete_file("/a.py")
    assert store.symbols_in("/a.py") == []
    assert store.imports_of("/a.py") == []
    assert store.calls_in("/a.py") == []
    assert store.all_indexed_paths() == []


def test_get_stale_paths_new_changed_unchanged_vanished():
    store = CodeMapStore()
    store.upsert_file(_extract("/a.py", content_hash="h_a"))
    store.upsert_file(_extract("/b.py", content_hash="h_b"))

    # a unchanged, b changed, c brand new; the vanished file "/b.py" gone from
    # current is simply absent (get_stale_paths only reports what's in current).
    current = {"/a.py": "h_a", "/b.py": "changed", "/c.py": "h_c"}
    stale = set(store.get_stale_paths(current))
    assert stale == {"/b.py", "/c.py"}  # a unchanged -> skipped

    # With nothing changed, warm store re-parses nothing.
    assert store.get_stale_paths({"/a.py": "h_a", "/b.py": "h_b"}) == []
