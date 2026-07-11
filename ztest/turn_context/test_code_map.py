#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for :class:`CodeMapContextSource` — the touched-set structure map feed.

The source drives a :class:`CodeMap` over the recently-touched files and pushes,
per turn, a small map of each file's defined symbols + within-set imports /
importers. These tests assert: no touched files stays silent, a real touched set
renders defines/imports/used-by, the block is change-gated (unchanged map stays
quiet, an edit re-surfaces), a compaction resets the frontier, relative-path
display honours cwd, and a raising CodeMap is swallowed (best-effort → None).
"""
from __future__ import annotations

import asyncio
import os

from mote.common.events import PostCompactEvent, SessionStartEvent
from mote.common.interface import EphemeralContextSource, ObservationSubscriber
from mote.context.turn_context import CodeMapContextSource


def run(coro):
    return asyncio.run(coro)


def _write(base, relpath: str, source: str) -> str:
    full = os.path.join(str(base), relpath)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(source)
    return full


def test_is_ephemeral_and_observation_source():
    src = CodeMapContextSource(get_touched_files=lambda: [])
    assert isinstance(src, EphemeralContextSource)
    assert isinstance(src, ObservationSubscriber)


def test_no_touched_files_returns_none():
    assert run(CodeMapContextSource(get_touched_files=lambda: []).render()) is None
    assert run(CodeMapContextSource(get_touched_files=lambda: None).render()) is None


def test_renders_defined_symbols(tmp_path):
    p = _write(tmp_path, "m.py", "def foo():\n    pass\n\nclass Bar:\n    def m(self):\n        pass\n")
    src = CodeMapContextSource(get_touched_files=lambda: [p])
    out = run(src.render())
    assert out is not None
    assert "# Code map" in out
    assert "foo" in out
    assert "Bar" in out
    assert "Bar.m" in out


def test_renders_within_set_imports_and_used_by(tmp_path):
    helper = _write(tmp_path, "helper.py", "def util():\n    pass\n")
    consumer = _write(tmp_path, "consumer.py", "import helper\n")
    src = CodeMapContextSource(get_touched_files=lambda: [helper, consumer])
    out = run(src.render())
    assert out is not None
    assert "imports:" in out
    assert "used by:" in out
    assert "helper.py" in out
    assert "consumer.py" in out


def test_ignores_files_with_no_structure(tmp_path):
    # A file with no symbols and no within-set edges yields no row.
    plain = _write(tmp_path, "empty.py", "x = 1\n")
    src = CodeMapContextSource(get_touched_files=lambda: [plain])
    assert run(src.render()) is None


def test_non_python_touched_file_silent(tmp_path):
    txt = _write(tmp_path, "notes.txt", "hello\n")
    src = CodeMapContextSource(get_touched_files=lambda: [txt])
    assert run(src.render()) is None


def test_change_gated_unchanged_map_quiet(tmp_path):
    p = _write(tmp_path, "m.py", "def foo():\n    pass\n")
    src = CodeMapContextSource(get_touched_files=lambda: [p])
    assert run(src.render()) is not None  # first turn: emitted
    assert run(src.render()) is None  # unchanged: quiet


def test_edit_resurfaces_map(tmp_path):
    p = _write(tmp_path, "m.py", "def foo():\n    pass\n")
    src = CodeMapContextSource(get_touched_files=lambda: [p])
    assert run(src.render()) is not None

    # Edit the file's symbol set; bump mtime distinctly so freshness fires.
    _write(tmp_path, "m.py", "def bar():\n    pass\n")
    st = os.stat(p)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    out = run(src.render())
    assert out is not None
    assert "bar" in out


def test_newly_touched_importer_resurfaces_target(tmp_path):
    helper = _write(tmp_path, "helper.py", "def util():\n    pass\n")
    touched = [helper]
    src = CodeMapContextSource(get_touched_files=lambda: list(touched))
    first = run(src.render())
    assert first is not None
    assert "used by:" not in first  # nobody imports it yet

    # A newly-touched consumer imports helper -> helper's used-by edge changes.
    consumer = _write(tmp_path, "consumer.py", "import helper\n")
    touched.append(consumer)
    out = run(src.render())
    assert out is not None
    assert "used by:" in out
    assert "consumer.py" in out


def test_postcompact_resets_frontier(tmp_path):
    p = _write(tmp_path, "m.py", "def foo():\n    pass\n")
    src = CodeMapContextSource(get_touched_files=lambda: [p])
    assert run(src.render()) is not None
    assert run(src.render()) is None  # change-gated quiet

    run(src.handle(PostCompactEvent()))  # compaction condensed the map away
    assert run(src.render()) is not None  # full map re-emitted


def test_other_events_ignored(tmp_path):
    p = _write(tmp_path, "m.py", "def foo():\n    pass\n")
    src = CodeMapContextSource(get_touched_files=lambda: [p])
    assert run(src.render()) is not None
    run(src.handle(SessionStartEvent(working_dir=str(tmp_path))))
    assert run(src.render()) is None  # unrelated event did not reset frontier


def test_relative_display_uses_cwd(tmp_path):
    helper = _write(tmp_path, "pkg/helper.py", "def util():\n    pass\n")
    consumer = _write(tmp_path, "pkg/consumer.py", "import helper\n")
    src = CodeMapContextSource(get_touched_files=lambda: [helper, consumer])
    out = run(src.render(cwd=str(tmp_path)))
    assert out is not None
    assert os.path.join("pkg", "helper.py") in out
    # The absolute prefix must not leak when a cwd is supplied.
    assert str(tmp_path) + os.sep + "pkg" not in out


def test_raising_code_map_swallowed(tmp_path):
    p = _write(tmp_path, "m.py", "def foo():\n    pass\n")

    class _Boom:
        def neighborhood(self, files):
            raise RuntimeError("boom")

    src = CodeMapContextSource(get_touched_files=lambda: [p], code_map=_Boom())
    assert run(src.render()) is None


# -- Feature 1: intra-file calls subline ---------------------------------------


def test_renders_calls_subline(tmp_path):
    p = _write(tmp_path, "c.py", "def helper():\n    pass\n\ndef run():\n    helper()\n")
    src = CodeMapContextSource(get_touched_files=lambda: [p])
    out = run(src.render())
    assert out is not None
    assert "calls:" in out
    assert "run" in out and "helper" in out


def test_signatures_shown_at_full_tier(tmp_path):
    p = _write(tmp_path, "c.py", "def run(x: int) -> None:\n    pass\n")
    src = CodeMapContextSource(get_touched_files=lambda: [p])
    out = run(src.render())
    assert out is not None
    assert "run(x: int) -> None" in out


# -- Feature 2 (Layer A): dangling-import subline ------------------------------


def test_renders_unread_imports_subline(tmp_path):
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/other.py", "def thing():\n    pass\n")
    consumer = _write(tmp_path, "pkg/consumer.py", "import pkg.other\n")
    src = CodeMapContextSource(get_touched_files=lambda: [consumer])
    out = run(src.render(cwd=str(tmp_path)))
    assert out is not None
    assert "also imports (unread):" in out
    assert os.path.join("pkg", "other.py") in out


def test_dangling_change_resurfaces_row(tmp_path):
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/other.py", "def thing():\n    pass\n")
    consumer = _write(tmp_path, "pkg/consumer.py", "x = 1\n")
    src = CodeMapContextSource(get_touched_files=lambda: [consumer])
    first = run(src.render())
    # consumer.py has no structure yet -> silent.
    assert first is None

    # Add a dangling import -> the row's signature changes -> it surfaces.
    _write(tmp_path, "pkg/consumer.py", "import pkg.other\n")
    st = os.stat(consumer)
    os.utime(consumer, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    out = run(src.render())
    assert out is not None
    assert "also imports (unread):" in out


# -- Feature 3: symbol folding -------------------------------------------------


def test_folds_large_symbol_list(tmp_path):
    # 20 top-level functions -> only the first 12 shown, rest folded.
    body = "".join(f"def f{i}():\n    pass\n\n" for i in range(20))
    p = _write(tmp_path, "big.py", body)
    src = CodeMapContextSource(get_touched_files=lambda: [p])
    out = run(src.render())
    assert out is not None
    assert "(+8 more)" in out
    assert "f0" in out
    # A folded-away symbol must not appear.
    assert "f19" not in out


# -- Feature 4: token-budget degradation ---------------------------------------


def test_tier_degrades_under_tiny_budget(tmp_path):
    p = _write(tmp_path, "c.py", "def helper():\n    pass\n\ndef run(x: int) -> None:\n    helper()\n")
    full = CodeMapContextSource(get_touched_files=lambda: [p])
    full_out = run(full.render())
    assert "run(x: int) -> None" in full_out  # signatures present at full tier
    assert "calls:" in full_out

    tight = CodeMapContextSource(get_touched_files=lambda: [p], max_tokens=5)
    tight_out = run(tight.render())
    assert tight_out is not None
    # Tier ≥1 drops signatures and the calls detail.
    assert "run(x: int) -> None" not in tight_out
    assert "calls:" not in tight_out
    # But the names and the file are still there (floor is always useful).
    assert "run" in tight_out
    assert "helper" in tight_out


def test_floor_emitted_even_when_overflowing(tmp_path):
    # A zero budget can never be met; the name-only floor must still render.
    p = _write(tmp_path, "c.py", "def run():\n    pass\n")
    src = CodeMapContextSource(get_touched_files=lambda: [p], max_tokens=0)
    out = run(src.render())
    assert out is not None
    assert "run" in out


# -- Layer B: LSP-resolved dangling-import symbols -----------------------------


class _FakeLspQuery:
    """Duck-typed lsp_query: returns a scripted documentSymbol table per path."""

    def __init__(self, by_path: dict) -> None:
        self._by_path = by_path
        self.asked: list[str] = []

    async def document_symbols(self, path: str) -> list:
        self.asked.append(path)
        return self._by_path.get(path, [])

    async def definition(self, path: str, line: int, character: int) -> list:
        return []


def test_unread_symbols_rendered_when_lsp_resolves(tmp_path):
    _write(tmp_path, "pkg/__init__.py", "")
    other = _write(tmp_path, "pkg/other.py", "def thing():\n    pass\n\ndef helper():\n    pass\n")
    consumer = _write(tmp_path, "pkg/consumer.py", "import pkg.other\n")
    lsp = _FakeLspQuery({other: [{"name": "thing"}, {"name": "helper"}]})
    src = CodeMapContextSource(get_touched_files=lambda: [consumer], lsp_query=lsp)
    out = run(src.render(cwd=str(tmp_path)))
    assert out is not None
    assert "also imports (unread):" in out
    # The resolved symbol names ride alongside the bare path.
    assert os.path.join("pkg", "other.py") in out
    assert "thing" in out and "helper" in out
    assert other in lsp.asked  # the dangling target was actually queried


def test_unread_symbols_fold_under_tight_budget(tmp_path):
    _write(tmp_path, "pkg/__init__.py", "")
    other = _write(tmp_path, "pkg/other.py", "def thing():\n    pass\n")
    consumer = _write(tmp_path, "pkg/consumer.py", "import pkg.other\n")
    lsp = _FakeLspQuery({other: [{"name": "thing"}]})
    src = CodeMapContextSource(get_touched_files=lambda: [consumer], lsp_query=lsp, max_tokens=1)
    out = run(src.render(cwd=str(tmp_path)))
    assert out is not None
    # Tier ≥1 drops the resolved-symbol annotation; the bare path remains.
    assert os.path.join("pkg", "other.py") in out
    assert "(thing)" not in out


def test_resolved_symbol_change_resurfaces_row(tmp_path):
    _write(tmp_path, "pkg/__init__.py", "")
    other = _write(tmp_path, "pkg/other.py", "def thing():\n    pass\n")
    consumer = _write(tmp_path, "pkg/consumer.py", "import pkg.other\n")
    by_path = {other: [{"name": "thing"}]}
    lsp = _FakeLspQuery(by_path)
    src = CodeMapContextSource(get_touched_files=lambda: [consumer], lsp_query=lsp)
    first = run(src.render(cwd=str(tmp_path)))
    assert first is not None and "thing" in first
    assert run(src.render(cwd=str(tmp_path))) is None  # unchanged: quiet

    # The target now resolves a different symbol table. The facade caches per
    # (target, mtime), so bump the target's mtime to invalidate that cache —
    # mirroring a real edit to the dangling file — then the signature changes.
    by_path[other] = [{"name": "thing"}, {"name": "extra"}]
    st = os.stat(other)
    os.utime(other, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    out = run(src.render(cwd=str(tmp_path)))
    assert out is not None
    assert "extra" in out


def test_lsp_query_off_reproduces_bare_path(tmp_path):
    # No lsp_query -> Layer B silent: the unread line shows the bare path only.
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/other.py", "def thing():\n    pass\n")
    consumer = _write(tmp_path, "pkg/consumer.py", "import pkg.other\n")
    src = CodeMapContextSource(get_touched_files=lambda: [consumer])
    out = run(src.render(cwd=str(tmp_path)))
    assert out is not None
    assert "also imports (unread):" in out
    assert "(thing)" not in out


# -- Layer C: whole-repo reverse dependencies ----------------------------------


class _FakeRepoIndex:
    """Duck-typed repo_index: reports whole-repo importers for any candidates."""

    def __init__(self, importers: list) -> None:
        self._importers = importers
        self.asked: list = []

    def importers(self, candidates) -> list:
        self.asked.append(set(candidates))
        return list(self._importers)


def test_repo_index_lists_untouched_importer(tmp_path):
    # helper is touched; an *untouched* repo file imports it. With a repo index,
    # used-by surfaces that untouched importer (whole-repo, locality dropped).
    helper = _write(tmp_path, "helper.py", "def util():\n    pass\n")
    untouched = _write(tmp_path, "faraway.py", "import helper\n")
    repo = _FakeRepoIndex([untouched])
    src = CodeMapContextSource(get_touched_files=lambda: [helper], repo_index=repo)
    out = run(src.render(cwd=str(tmp_path)))
    assert out is not None
    assert "used by:" in out
    assert "faraway.py" in out  # the untouched importer is surfaced
    assert repo.asked  # the repo index was consulted


def test_repo_index_self_excluded(tmp_path):
    # The repo index may return the file itself; it must be filtered from used-by.
    helper = _write(tmp_path, "helper.py", "def util():\n    pass\n")
    repo = _FakeRepoIndex([helper])
    src = CodeMapContextSource(get_touched_files=lambda: [helper], repo_index=repo)
    out = run(src.render(cwd=str(tmp_path)))
    # Only structure was the definition; self-import filtered -> no used-by line.
    assert out is not None
    assert "used by:" not in out


def test_repo_index_off_uses_touched_set(tmp_path):
    # No repo_index -> used-by stays touched-set-scoped (backward compatible).
    helper = _write(tmp_path, "helper.py", "def util():\n    pass\n")
    consumer = _write(tmp_path, "consumer.py", "import helper\n")
    src = CodeMapContextSource(get_touched_files=lambda: [helper, consumer])
    out = run(src.render())
    assert out is not None
    assert "used by:" in out
    assert "consumer.py" in out


def test_repo_importer_change_resurfaces_row(tmp_path):
    helper = _write(tmp_path, "helper.py", "def util():\n    pass\n")
    a = _write(tmp_path, "a.py", "import helper\n")
    b = _write(tmp_path, "b.py", "import helper\n")
    importers = [a]
    repo = _FakeRepoIndex(importers)
    src = CodeMapContextSource(get_touched_files=lambda: [helper], repo_index=repo)
    first = run(src.render(cwd=str(tmp_path)))
    assert first is not None and "a.py" in first
    assert run(src.render(cwd=str(tmp_path))) is None  # unchanged: quiet

    # A new whole-repo importer appears -> used-by edge changes -> re-surfaces.
    importers.append(b)
    out = run(src.render(cwd=str(tmp_path)))
    assert out is not None
    assert "b.py" in out


# -- Feature 1: read-state-gated self-description ------------------------------


def _bump_mtime(path: str) -> int:
    """Bump *path*'s mtime by a distinct second and return the new mtime_ns."""
    st = os.stat(path)
    new_ns = st.st_mtime_ns + 1_000_000_000
    os.utime(path, ns=(st.st_atime_ns, new_ns))
    return os.stat(path).st_mtime_ns


def test_in_context_file_hides_defines_keeps_edges(tmp_path):
    # helper is read at its current mtime -> its body is in context -> defines
    # suppressed; but the used-by edge (never in the body) still renders.
    helper = _write(tmp_path, "helper.py", "def util():\n    pass\n")
    consumer = _write(tmp_path, "consumer.py", "import helper\n")
    read_state = {helper: os.stat(helper).st_mtime_ns}
    src = CodeMapContextSource(
        get_touched_files=lambda: [helper, consumer],
        get_read_state=lambda: dict(read_state),
    )
    out = run(src.render())
    assert out is not None
    # helper's defines are suppressed (body in context)...
    assert "util" not in out
    # ...but its edge (used by consumer) still shows.
    assert "used by:" in out
    assert "consumer.py" in out


def test_dependency_only_file_still_shows_defines(tmp_path):
    # A file with no read entry (surfaced purely as a dependency) shows defines.
    helper = _write(tmp_path, "helper.py", "def util():\n    pass\n")
    src = CodeMapContextSource(
        get_touched_files=lambda: [helper],
        get_read_state=lambda: {},  # helper never read
    )
    out = run(src.render())
    assert out is not None
    assert "util" in out


def test_no_read_state_reproduces_defines(tmp_path):
    # No get_read_state -> current behavior: defines always shown.
    helper = _write(tmp_path, "helper.py", "def util():\n    pass\n")
    src = CodeMapContextSource(get_touched_files=lambda: [helper])
    out = run(src.render())
    assert out is not None
    assert "util" in out


def test_in_context_file_no_edges_dropped(tmp_path):
    # An in-context file with no edges has nothing left to say -> row dropped.
    p = _write(tmp_path, "m.py", "def foo():\n    pass\n")
    read_state = {p: os.stat(p).st_mtime_ns}
    src = CodeMapContextSource(
        get_touched_files=lambda: [p],
        get_read_state=lambda: dict(read_state),
    )
    assert run(src.render()) is None


def test_postcompact_reshows_defines_for_in_context_file(tmp_path):
    p = _write(tmp_path, "m.py", "def foo():\n    pass\n")
    helper = _write(tmp_path, "helper.py", "import m\n")  # gives m an edge so row survives
    read_state = {p: os.stat(p).st_mtime_ns}
    src = CodeMapContextSource(
        get_touched_files=lambda: [p, helper],
        get_read_state=lambda: dict(read_state),
    )
    first = run(src.render())
    assert first is not None
    assert "foo" not in first  # in-context -> defines suppressed

    run(src.handle(PostCompactEvent()))  # bodies condensed away
    out = run(src.render())
    assert out is not None
    assert "foo" in out  # frontier cleared -> defines re-appear


def test_stale_read_shows_defines(tmp_path):
    # Read entry exists but the file changed since -> not in context -> defines.
    helper = _write(tmp_path, "helper.py", "def util():\n    pass\n")
    consumer = _write(tmp_path, "consumer.py", "import helper\n")
    stale_mtime = os.stat(helper).st_mtime_ns
    _write(tmp_path, "helper.py", "def util():\n    pass\n\ndef more():\n    pass\n")
    _bump_mtime(helper)
    src = CodeMapContextSource(
        get_touched_files=lambda: [helper, consumer],
        get_read_state=lambda: {helper: stale_mtime},  # recorded at old version
    )
    out = run(src.render())
    assert out is not None
    assert "util" in out  # stale read -> body not trusted -> defines shown


# -- Feature 3: interface-change risk label ------------------------------------


def test_signature_change_flags_interface_changed(tmp_path):
    p = _write(tmp_path, "api.py", "def foo(x):\n    pass\n")
    src = CodeMapContextSource(get_touched_files=lambda: [p])
    assert run(src.render()) is not None  # baseline established

    # Change foo's signature -> breaking for callers.
    _write(tmp_path, "api.py", "def foo(x, y):\n    pass\n")
    _bump_mtime(p)
    out = run(src.render())
    assert out is not None
    assert "interface changed" in out
    assert "foo" in out


def test_removed_symbol_flags_interface_changed(tmp_path):
    p = _write(tmp_path, "api.py", "def foo():\n    pass\n\ndef bar():\n    pass\n")
    src = CodeMapContextSource(get_touched_files=lambda: [p])
    assert run(src.render()) is not None

    _write(tmp_path, "api.py", "def foo():\n    pass\n")  # bar removed
    _bump_mtime(p)
    out = run(src.render())
    assert out is not None
    assert "interface changed" in out
    assert "bar" in out


def test_added_symbol_does_not_flag_interface_changed(tmp_path):
    p = _write(tmp_path, "api.py", "def foo():\n    pass\n")
    src = CodeMapContextSource(get_touched_files=lambda: [p])
    assert run(src.render()) is not None

    _write(tmp_path, "api.py", "def foo():\n    pass\n\ndef added():\n    pass\n")
    _bump_mtime(p)
    out = run(src.render())
    assert out is not None
    # A pure addition is non-breaking: no risk label.
    assert "interface changed" not in out


def test_private_symbol_change_does_not_flag_interface_changed(tmp_path):
    # A leading-underscore top-level symbol is not part of the importable surface,
    # so changing its signature must not trip the risk label.
    p = _write(tmp_path, "api.py", "def _helper(x):\n    pass\n")
    src = CodeMapContextSource(get_touched_files=lambda: [p])
    assert run(src.render()) is not None

    _write(tmp_path, "api.py", "def _helper(x, y):\n    pass\n")
    _bump_mtime(p)
    out = run(src.render())
    # Private-only churn: no public contract broke -> no label (and no re-surface).
    if out is not None:
        assert "interface changed" not in out


def test_method_signature_change_does_not_flag_interface_changed(tmp_path):
    # A method (nested, dotted qualified name) is reached through its owner, not
    # imported directly -> a method-signature change is not a module contract break.
    p = _write(tmp_path, "api.py", "class C:\n    def m(self, x):\n        pass\n")
    src = CodeMapContextSource(get_touched_files=lambda: [p])
    assert run(src.render()) is not None

    _write(tmp_path, "api.py", "class C:\n    def m(self, x, y):\n        pass\n")
    _bump_mtime(p)
    out = run(src.render())
    if out is not None:
        assert "interface changed" not in out


def test_risk_label_exempt_from_tiny_budget(tmp_path):
    helper = _write(tmp_path, "api.py", "def foo(x):\n    pass\n")
    consumer = _write(tmp_path, "consumer.py", "import api\n")
    src = CodeMapContextSource(get_touched_files=lambda: [helper, consumer], max_tokens=1)
    assert run(src.render()) is not None

    _write(tmp_path, "api.py", "def foo(x, y):\n    pass\n")
    _bump_mtime(helper)
    out = run(src.render())
    assert out is not None
    # Even under a tiny budget the ⚠ label + used-by survive.
    assert "interface changed" in out
    assert "used by:" in out
    assert "consumer.py" in out


def test_interface_change_resurfaces_row(tmp_path):
    p = _write(tmp_path, "api.py", "def foo(x):\n    pass\n")
    src = CodeMapContextSource(get_touched_files=lambda: [p])
    assert run(src.render()) is not None
    assert run(src.render()) is None  # change-gated quiet

    _write(tmp_path, "api.py", "def foo(x, y):\n    pass\n")
    _bump_mtime(p)
    out = run(src.render())
    assert out is not None
    assert "interface changed" in out


# -- Feature 2: hybrid on-demand LSP references --------------------------------


class _FakeRefLspQuery:
    """Duck-typed lsp_query: scripted references per (path); counts calls."""

    def __init__(self, refs: list) -> None:
        self._refs = refs
        self.ref_calls: list = []

    async def document_symbols(self, path: str) -> list:
        return []

    async def definition(self, path: str, line: int, character: int) -> list:
        return []

    async def references(self, path: str, line: int, character: int) -> list:
        self.ref_calls.append((path, line, character))
        return list(self._refs)


def _uri(path: str) -> str:
    from pathlib import Path

    return Path(path).as_uri()


def test_precise_callers_rendered_on_interface_change(tmp_path):
    api = _write(tmp_path, "api.py", "def foo(x):\n    pass\n")
    caller = _write(tmp_path, "caller.py", "import api\n")
    lsp = _FakeRefLspQuery([{"uri": _uri(caller)}])
    src = CodeMapContextSource(get_touched_files=lambda: [api], lsp_query=lsp)
    assert run(src.render(cwd=str(tmp_path))) is not None  # baseline

    _write(tmp_path, "api.py", "def foo(x, y):\n    pass\n")
    _bump_mtime(api)
    out = run(src.render(cwd=str(tmp_path)))
    assert out is not None
    assert "interface changed" in out
    assert "foo called by:" in out
    assert "caller.py" in out
    assert lsp.ref_calls  # references was actually queried


def test_precise_callers_fallback_to_string_index(tmp_path):
    # LSP returns no references -> fall back to string-index used-by.
    api = _write(tmp_path, "api.py", "def foo(x):\n    pass\n")
    consumer = _write(tmp_path, "consumer.py", "import api\n")
    lsp = _FakeRefLspQuery([])  # no references
    src = CodeMapContextSource(get_touched_files=lambda: [api, consumer], lsp_query=lsp)
    assert run(src.render(cwd=str(tmp_path))) is not None

    _write(tmp_path, "api.py", "def foo(x, y):\n    pass\n")
    _bump_mtime(api)
    out = run(src.render(cwd=str(tmp_path)))
    assert out is not None
    assert "interface changed" in out
    assert "used by:" in out
    assert "consumer.py" in out


def test_precise_callers_off_without_lsp(tmp_path):
    # No lsp_query -> interface change still labelled, used-by from string index.
    api = _write(tmp_path, "api.py", "def foo(x):\n    pass\n")
    consumer = _write(tmp_path, "consumer.py", "import api\n")
    src = CodeMapContextSource(get_touched_files=lambda: [api, consumer])
    assert run(src.render(cwd=str(tmp_path))) is not None

    _write(tmp_path, "api.py", "def foo(x, y):\n    pass\n")
    _bump_mtime(api)
    out = run(src.render(cwd=str(tmp_path)))
    assert out is not None
    assert "interface changed" in out
    assert "called by:" not in out  # no precise callers path
    assert "used by:" in out


def test_precise_callers_cached_per_version(tmp_path):
    api = _write(tmp_path, "api.py", "def foo(x):\n    pass\n")
    caller = _write(tmp_path, "caller.py", "import api\n")
    lsp = _FakeRefLspQuery([{"uri": _uri(caller)}])
    src = CodeMapContextSource(get_touched_files=lambda: [api], lsp_query=lsp)
    run(src.render(cwd=str(tmp_path)))  # baseline

    _write(tmp_path, "api.py", "def foo(x, y):\n    pass\n")
    _bump_mtime(api)
    run(src.render(cwd=str(tmp_path)))  # interface change -> references queried
    calls_after_first = len(lsp.ref_calls)
    assert calls_after_first >= 1

    # Re-render at the SAME version: the row is change-gated quiet, and even if it
    # weren't, the (path, symbol, content_hash) cache means no new references call.
    run(src.handle(PostCompactEvent()))  # force re-emit (frontier reset)
    run(src.render(cwd=str(tmp_path)))
    assert len(lsp.ref_calls) == calls_after_first  # served from cache


def test_ref_symbol_query_capped(tmp_path):
    # More than _MAX_REF_SYMBOLS symbols change signature at once -> queries capped.
    body = "".join(f"def f{i}(a):\n    pass\n\n" for i in range(10))
    api = _write(tmp_path, "api.py", body)
    lsp = _FakeRefLspQuery([])
    src = CodeMapContextSource(get_touched_files=lambda: [api], lsp_query=lsp)
    run(src.render())  # baseline

    body2 = "".join(f"def f{i}(a, b):\n    pass\n\n" for i in range(10))  # all sigs changed
    _write(tmp_path, "api.py", body2)
    _bump_mtime(api)
    run(src.render())
    from mote.context.code_map import CodeMap

    assert len(lsp.ref_calls) <= CodeMap._MAX_REF_SYMBOLS


# -- P1: docstring summaries in the rendered map ------------------------------


def test_module_purpose_line_rendered(tmp_path):
    p = _write(tmp_path, "m.py", '"""What this module is for."""\ndef foo():\n    pass\n')
    src = CodeMapContextSource(get_touched_files=lambda: [p])
    out = run(src.render())
    assert "purpose: What this module is for." in out


def test_symbol_summaries_expanded_per_line(tmp_path):
    src_txt = (
        '"""Mod."""\n'
        'def foo(a):\n    """Foo does foo."""\n    pass\n\n'
        'def bar():\n    """Bar does bar."""\n    pass\n'
    )
    p = _write(tmp_path, "m.py", src_txt)
    src = CodeMapContextSource(get_touched_files=lambda: [p])
    out = run(src.render())
    assert "foo(a) — Foo does foo." in out
    assert "bar() — Bar does bar." in out


def test_undocumented_symbols_fall_back_to_compact_line(tmp_path):
    # No docstrings anywhere -> the compact single "defines: a, b" form (no
    # per-symbol expansion, no summaries).
    p = _write(tmp_path, "m.py", "def foo(a):\n    pass\n\ndef bar():\n    pass\n")
    src = CodeMapContextSource(get_touched_files=lambda: [p])
    out = run(src.render())
    assert "defines: foo(a), bar()" in out


def test_docstring_edit_resurfaces_row(tmp_path):
    p = _write(tmp_path, "m.py", '"""One."""\ndef foo():\n    """First."""\n    pass\n')
    src = CodeMapContextSource(get_touched_files=lambda: [p])
    assert run(src.render()) is not None  # first emit
    assert run(src.render()) is None  # unchanged -> quiet

    _write(tmp_path, "m.py", '"""Two."""\ndef foo():\n    """Second."""\n    pass\n')
    _bump_mtime(p)
    out = run(src.render())
    assert out is not None
    assert "purpose: Two." in out
    assert "Second." in out


# -- P2: glimpsed files fold into the map -------------------------------------


def test_glimpsed_file_rendered_with_defines(tmp_path):
    # A file only *glimpsed* (search hit, never read) still gets its structure
    # surfaced so the model can decide whether to open it.
    g = _write(tmp_path, "hit.py", '"""Search hit module."""\ndef target():\n    pass\n')
    src = CodeMapContextSource(get_touched_files=lambda: [], get_glimpsed_files=lambda: [g])
    out = run(src.render())
    assert out is not None
    assert "hit.py" in out
    assert "target" in out
    assert "purpose: Search hit module." in out


def test_glimpsed_deduped_against_touched(tmp_path):
    # A file that is both read and glimpsed appears once (read wins its slot).
    p = _write(tmp_path, "m.py", "def foo():\n    pass\n")
    src = CodeMapContextSource(get_touched_files=lambda: [p], get_glimpsed_files=lambda: [p])
    out = run(src.render())
    assert out is not None
    assert out.count("m.py") == 1


def test_no_files_when_both_empty(tmp_path):
    src = CodeMapContextSource(get_touched_files=lambda: [], get_glimpsed_files=lambda: [])
    assert run(src.render()) is None


def test_glimpsed_provider_raise_is_swallowed(tmp_path):
    p = _write(tmp_path, "m.py", "def foo():\n    pass\n")

    def boom():
        raise RuntimeError("nope")

    src = CodeMapContextSource(get_touched_files=lambda: [p], get_glimpsed_files=boom)
    out = run(src.render())  # touched file still renders; glimpse failure ignored
    assert out is not None
    assert "foo" in out


# -- P3: opportunistic per-symbol callers of calm public symbols ---------------


def test_surface_callers_off_by_default(tmp_path):
    # A calm public symbol is NOT queried for callers unless the flag is on — the
    # default keeps the extra LSP references volume opt-in.
    api = _write(tmp_path, "api.py", "def foo(x):\n    pass\n")
    caller = _write(tmp_path, "caller.py", "import api\n")
    lsp = _FakeRefLspQuery([{"uri": _uri(caller)}])
    src = CodeMapContextSource(get_touched_files=lambda: [api], lsp_query=lsp)
    out = run(src.render(cwd=str(tmp_path)))
    assert out is not None
    assert "called by:" not in out
    assert lsp.ref_calls == []  # references never issued for a calm row


def test_surface_callers_renders_for_calm_public_symbol(tmp_path):
    # With the flag on, a calm public symbol's real callers surface without any
    # interface change — the reverse call direction the model otherwise lacks.
    api = _write(tmp_path, "api.py", "def foo(x):\n    pass\n")
    caller = _write(tmp_path, "caller.py", "import api\n")
    lsp = _FakeRefLspQuery([{"uri": _uri(caller)}])
    src = CodeMapContextSource(get_touched_files=lambda: [api], lsp_query=lsp, surface_callers=True)
    out = run(src.render(cwd=str(tmp_path)))
    assert out is not None
    assert "interface changed" not in out  # no edit — this is the calm path
    assert "foo called by: caller.py" in out
    assert lsp.ref_calls  # references was actually queried


def test_surface_callers_needs_lsp(tmp_path):
    # Flag on but no LSP facade -> no callers path (nothing to resolve them with).
    api = _write(tmp_path, "api.py", "def foo(x):\n    pass\n")
    _write(tmp_path, "caller.py", "import api\n")
    src = CodeMapContextSource(get_touched_files=lambda: [api], surface_callers=True)
    out = run(src.render(cwd=str(tmp_path)))
    assert out is not None
    assert "called by:" not in out


def test_surface_callers_skips_private_and_nested(tmp_path):
    # Only public top-level symbols are a caller's import surface; a private def
    # and a method must not be queried for callers.
    api = _write(tmp_path, "api.py", "def _hidden():\n    pass\n\nclass C:\n    def m(self):\n        pass\n")
    lsp = _FakeRefLspQuery([])
    src = CodeMapContextSource(get_touched_files=lambda: [api], lsp_query=lsp, surface_callers=True)
    out = run(src.render(cwd=str(tmp_path)))
    assert out is not None
    # Only the public class C is queryable; _hidden and C.m are filtered out.
    assert lsp.ref_calls  # C was queried
    assert len(lsp.ref_calls) == 1


def test_surface_callers_capped(tmp_path):
    # More than _MAX_REF_SYMBOLS public symbols -> the reference queries are capped.
    body = "".join(f"def f{i}():\n    pass\n\n" for i in range(10))
    api = _write(tmp_path, "api.py", body)
    lsp = _FakeRefLspQuery([])
    src = CodeMapContextSource(get_touched_files=lambda: [api], lsp_query=lsp, surface_callers=True)
    run(src.render())
    from mote.context.code_map import CodeMap

    assert len(lsp.ref_calls) <= CodeMap._MAX_REF_SYMBOLS


def test_surface_callers_cached_per_version(tmp_path):
    # The (path, symbol, content_hash) cache means a re-render at the same version
    # issues no new references call — even after a forced frontier reset.
    api = _write(tmp_path, "api.py", "def foo():\n    pass\n")
    caller = _write(tmp_path, "caller.py", "import api\n")
    lsp = _FakeRefLspQuery([{"uri": _uri(caller)}])
    src = CodeMapContextSource(get_touched_files=lambda: [api], lsp_query=lsp, surface_callers=True)
    run(src.render(cwd=str(tmp_path)))
    first = len(lsp.ref_calls)
    assert first >= 1

    run(src.handle(PostCompactEvent()))  # force re-emit (frontier reset)
    run(src.render(cwd=str(tmp_path)))
    assert len(lsp.ref_calls) == first  # served from cache, no new query


def test_surfaced_caller_change_resurfaces_row(tmp_path):
    # A newly-appearing caller changes the row's signature so it re-surfaces even
    # though the file's own symbols/edges are unchanged.
    api = _write(tmp_path, "api.py", "def foo():\n    pass\n")
    a = _write(tmp_path, "a.py", "import api\n")
    b = _write(tmp_path, "b.py", "import api\n")
    refs = [{"uri": _uri(a)}]
    lsp = _FakeRefLspQuery(refs)
    src = CodeMapContextSource(get_touched_files=lambda: [api], lsp_query=lsp, surface_callers=True)
    first = run(src.render(cwd=str(tmp_path)))
    assert first is not None and "foo called by: a.py" in first
    assert run(src.render(cwd=str(tmp_path))) is None  # unchanged -> quiet

    # A new caller appears. The refs cache is keyed on content_hash, so a content
    # edit (kept non-breaking: foo's signature is unchanged) invalidates it and
    # forces a re-query; the surfaced-callers fold then re-surfaces the row.
    refs.append({"uri": _uri(b)})
    _write(tmp_path, "api.py", "# touched\ndef foo():\n    pass\n")
    _bump_mtime(api)
    out = run(src.render(cwd=str(tmp_path)))
    assert out is not None
    assert "b.py" in out
