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

from metagpt.common.events import PostCompactEvent, SessionStartEvent
from metagpt.common.interface import EphemeralContextSource, ObservationSubscriber
from metagpt.context.turn_context import CodeMapContextSource


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
