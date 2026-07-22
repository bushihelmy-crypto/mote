"""Tests for CodeMapStore — SQLite nodes/edges persistence + neighborhood reads."""

from __future__ import annotations

from mote.context.code_map.extractor import CallEdge, CodeMapExtractor, FileExtract, ImportBinding, Symbol
from mote.context.code_map.store import CodeMapStore


def _extract(
    path: str,
    *,
    symbols=None,
    imports=None,
    calls=None,
    import_bindings=None,
    content_hash="",
    module_summary="",
) -> FileExtract:
    return FileExtract(
        path=path,
        module_summary=module_summary,
        symbols=symbols or [],
        imports=imports or [],
        calls=calls or [],
        import_bindings=import_bindings or [],
        content_hash=content_hash,
    )


def _real_extract(tmp_path, name: str, source: str) -> FileExtract:
    """A genuine extract (scope graph + bindings) for round-trip resolution tests."""
    p = tmp_path / name
    p.write_text(source, encoding="utf-8")
    return CodeMapExtractor().extract(str(p))


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


# -- P1: summary persistence + migration --------------------------------------


def test_symbol_summary_roundtrip():
    store = CodeMapStore()
    store.upsert_file(
        _extract(
            "/a.py",
            symbols=[Symbol(name="f", qualified_name="f", kind="function", start_line=1, summary="Does a thing.")],
        )
    )
    got = store.symbols_in("/a.py")
    assert got[0].summary == "Does a thing."


def test_module_summary_roundtrip():
    store = CodeMapStore()
    store.upsert_file(_extract("/a.py", module_summary="Module intent."))
    assert store.module_summary_of("/a.py") == "Module intent."


def test_module_summary_empty_for_unknown_or_undocumented():
    store = CodeMapStore()
    assert store.module_summary_of("/missing.py") == ""
    store.upsert_file(_extract("/a.py"))  # no summary
    assert store.module_summary_of("/a.py") == ""


def test_migration_adds_columns_to_legacy_db(tmp_path):
    # A DB created without the summary columns (an older build) is migrated on
    # open: ADD COLUMN runs, and upsert/read of the new fields works.
    import sqlite3

    db = str(tmp_path / "legacy.db")
    con = sqlite3.connect(db)
    # The pre-summary shape: nodes/files without the summary columns (edges match
    # the current schema, which is created idempotently on open).
    con.executescript(
        "CREATE TABLE nodes (file_path TEXT NOT NULL, name TEXT NOT NULL, "
        "qualified_name TEXT NOT NULL, kind TEXT NOT NULL, start_line INTEGER NOT NULL, "
        "signature TEXT NOT NULL DEFAULT '');"
        "CREATE TABLE files (path TEXT PRIMARY KEY, content_hash TEXT NOT NULL, indexed_at INTEGER);"
    )
    con.commit()
    con.close()

    store = CodeMapStore(db)  # opening runs _migrate()
    store.upsert_file(
        _extract(
            "/a.py",
            module_summary="Migrated module.",
            symbols=[Symbol(name="f", qualified_name="f", kind="function", start_line=1, summary="Migrated sym.")],
        )
    )
    assert store.module_summary_of("/a.py") == "Migrated module."
    assert store.symbols_in("/a.py")[0].summary == "Migrated sym."


def test_migration_folds_legacy_edges_into_imports_and_calls(tmp_path):
    # A DB from the original build stored imports+calls in one `edges` table.
    # Opening it must fold those rows into the dedicated imports/calls tables and
    # drop `edges`, so a warm store keeps its reverse-dep data across the upgrade.
    import sqlite3

    db = str(tmp_path / "edges.db")
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE edges (source_file TEXT NOT NULL, target TEXT NOT NULL, kind TEXT NOT NULL, "
        "caller TEXT NOT NULL DEFAULT '', line INTEGER NOT NULL DEFAULT 0);"
    )
    con.execute("INSERT INTO edges (source_file, target, kind, caller, line) VALUES ('/a.py', 'os', 'imports', '', 0)")
    con.execute(
        "INSERT INTO edges (source_file, target, kind, caller, line) VALUES ('/a.py', 'helper', 'calls', 'run', 5)"
    )
    con.commit()
    con.close()

    store = CodeMapStore(db)  # opening runs _migrate() -> _migrate_edges()
    assert store.imports_of("/a.py") == ["os"]
    calls = store.calls_in("/a.py")
    assert [(c.caller, c.callee, c.line) for c in calls] == [("run", "helper", 5)]
    # `edges` is gone; a second open is a no-op (idempotent).
    assert store._conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'edges'").fetchone() is None
    CodeMapStore(db)  # does not raise


# -- resolution layer: scope graph + symbol bindings --------------------------


def test_graph_of_roundtrips_scopes_and_defs(tmp_path):
    store = CodeMapStore()
    ex = _real_extract(tmp_path, "g.py", "def helper():\n    pass\n\ndef run():\n    helper()\n")
    store.upsert_file(ex)
    graph = store.graph_of(ex.path)
    # module scope + the two function body scopes survive the round-trip.
    assert any(s.kind == "module" for s in graph.scopes.values())
    def_names = {d.name for d in graph.defs}
    assert {"helper", "run"} <= def_names
    # The reconstructed graph still resolves the intra-file call edge.
    edges = [(o.qualified_name if o else "", c.name) for o, c, _ in graph.call_edges()]
    assert ("run", "helper") in edges


def test_graph_of_empty_for_unindexed_file():
    store = CodeMapStore()
    graph = store.graph_of("/never.py")
    assert graph.scopes == {} and graph.defs == [] and graph.refs == []


def test_import_bindings_roundtrip(tmp_path):
    store = CodeMapStore()
    ex = _real_extract(tmp_path, "ib.py", "from pkg.other import thing as t\nimport os\n")
    store.upsert_file(ex)
    got = {b.local_name: b for b in store.import_bindings_of(ex.path)}
    assert got["t"].module == "pkg.other"
    assert got["t"].imported_name == "thing"
    assert got["os"].module == "os"
    assert got["os"].imported_name == ""


def test_definition_of_returns_top_level_symbol(tmp_path):
    store = CodeMapStore()
    ex = _real_extract(tmp_path, "d.py", "def thing():\n    pass\n\nclass Widget:\n    pass\n")
    store.upsert_file(ex)
    d = store.definition_of(ex.path, "thing")
    assert d is not None and d.kind == "function"
    w = store.definition_of(ex.path, "Widget")
    assert w is not None and w.kind == "class"


def test_definition_of_none_for_unknown_or_method(tmp_path):
    store = CodeMapStore()
    # `m` is a method (class scope), not an importable top-level symbol.
    ex = _real_extract(tmp_path, "d2.py", "class C:\n    def m(self):\n        pass\n")
    store.upsert_file(ex)
    assert store.definition_of(ex.path, "nope") is None
    assert store.definition_of(ex.path, "m") is None


def test_references_to_intra_file(tmp_path):
    store = CodeMapStore()
    ex = _real_extract(
        tmp_path,
        "r.py",
        "def helper():\n    pass\n\ndef a():\n    helper()\n\ndef b():\n    helper()\n",
    )
    store.upsert_file(ex)
    refs = store.references_to(ex.path, "helper")
    # two call sites reference the same top-level def.
    lines = sorted(line for _, line in refs)
    assert len(lines) == 2
    assert all(p == ex.path for p, _ in refs)


def test_symbol_importers_matches_module_and_symbol(tmp_path):
    store = CodeMapStore()
    consumer = _real_extract(tmp_path, "consumer.py", "from pkg.other import thing\n")
    unrelated = _real_extract(tmp_path, "unrelated.py", "from pkg.other import widget\n")
    store.upsert_file(consumer)
    store.upsert_file(unrelated)
    # only the file importing the *specific* symbol comes back.
    got = store.symbol_importers(["pkg.other"], "thing")
    assert [p for p, _ in got] == [consumer.path]
    # a symbol nobody imports -> empty.
    assert store.symbol_importers(["pkg.other"], "absent") == []
    # empty modules / empty symbol -> empty.
    assert store.symbol_importers([], "thing") == []
    assert store.symbol_importers(["pkg.other"], "") == []


def test_delete_file_clears_resolution_tables(tmp_path):
    store = CodeMapStore()
    ex = _real_extract(tmp_path, "del.py", "from pkg import thing\n\ndef run():\n    thing()\n")
    store.upsert_file(ex)
    assert store.graph_of(ex.path).defs != []
    assert store.import_bindings_of(ex.path) != []
    store.delete_file(ex.path)
    assert store.graph_of(ex.path).defs == []
    assert store.import_bindings_of(ex.path) == []
    assert store.references_to(ex.path, "run") == []


# -- multi-language: language + skip_class_scope round-trip -------------------


def test_language_and_skip_class_scope_roundtrip():
    from mote.context.code_map.scopes import Scope, ScopeGraph

    store = CodeMapStore()
    graph = ScopeGraph(
        scopes={0: Scope(id=0, kind="module", parent=None, start_line=1)},
        defs=[],
        refs=[],
        skip_class_scope=False,
    )
    ex = FileExtract(path="/x.go", language="go", scope_graph=graph, content_hash="h")
    store.upsert_file(ex)
    # files row carries language + policy.
    row = store._conn.execute("SELECT language, skip_class_scope FROM files WHERE path = ?", ("/x.go",)).fetchone()
    assert row == ("go", 0)
    # graph_of rehydrates the class-skip policy.
    assert store.graph_of("/x.go").skip_class_scope is False


def test_python_defaults_skip_class_scope_true():
    from mote.context.code_map.scopes import Scope, ScopeGraph

    store = CodeMapStore()
    graph = ScopeGraph(scopes={0: Scope(id=0, kind="module", parent=None, start_line=1)}, defs=[], refs=[])
    store.upsert_file(FileExtract(path="/a.py", language="python", scope_graph=graph))
    assert store.graph_of("/a.py").skip_class_scope is True


def test_old_db_reads_defaults_for_new_columns():
    # Simulate a pre-multilang DB: a files table lacking language/skip_class_scope.
    import sqlite3
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        dbpath = tf.name
    conn = sqlite3.connect(dbpath)
    conn.execute(
        "CREATE TABLE files (path TEXT PRIMARY KEY, content_hash TEXT NOT NULL, "
        "indexed_at INTEGER, module_summary TEXT NOT NULL DEFAULT '')"
    )
    conn.execute("INSERT INTO files (path, content_hash) VALUES ('/old.py', 'h')")
    conn.commit()
    conn.close()
    # Opening with the current store migrates the columns in with defaults.
    store = CodeMapStore(dbpath)
    row = store._conn.execute("SELECT language, skip_class_scope FROM files WHERE path = ?", ("/old.py",)).fetchone()
    assert row == ("", 1)  # Python-equivalent defaults


def test_widened_symbol_kinds_persist():
    store = CodeMapStore()
    ex = _extract(
        "/x.rs",
        symbols=[
            Symbol(name="S", qualified_name="S", kind="struct", start_line=1),
            Symbol(name="T", qualified_name="T", kind="trait", start_line=2),
            Symbol(name="I", qualified_name="I", kind="interface", start_line=3),
        ],
    )
    store.upsert_file(ex)
    kinds = {s.kind for s in store.symbols_in("/x.rs")}
    assert kinds == {"struct", "trait", "interface"}
