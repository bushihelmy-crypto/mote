"""Tests for CodeMapStore — SQLite nodes/edges persistence + neighborhood reads."""

from __future__ import annotations

from metagpt.context.code_map.extractor import CallEdge, FileExtract, Symbol
from metagpt.context.code_map.store import CodeMapStore


def _extract(path: str, *, symbols=None, imports=None, calls=None) -> FileExtract:
    return FileExtract(
        path=path,
        symbols=symbols or [],
        imports=imports or [],
        calls=calls or [],
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
