#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for :class:`ChangedFilesContextSource` — external-edit freshness feed.

The source compares each tracked file's current on-disk mtime against the mtime
recorded when the agent last read it, and reports the ones that changed. These
tests assert: unchanged/gone files stay silent, an external change surfaces once
(change-gated), a re-change re-announces, and relative-path display honours cwd.
"""
from __future__ import annotations

import asyncio
import os

from metagpt.common.interface import EphemeralContextSource
from metagpt.context.turn_context import ChangedFilesContextSource


def run(coro):
    return asyncio.run(coro)


def _mtime(path):
    return os.stat(path).st_mtime_ns


def test_is_ephemeral_context_source():
    assert isinstance(ChangedFilesContextSource(lambda: {}), EphemeralContextSource)


def test_empty_state_returns_none():
    assert run(ChangedFilesContextSource(lambda: {}).render()) is None
    assert run(ChangedFilesContextSource(lambda: None).render()) is None


def test_unchanged_file_silent(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("body")
    src = ChangedFilesContextSource(lambda: {str(p): _mtime(p)})
    assert run(src.render()) is None


def test_missing_file_silent(tmp_path):
    gone = str(tmp_path / "gone.py")
    src = ChangedFilesContextSource(lambda: {gone: 12345})
    assert run(src.render()) is None


def test_external_change_reported(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("old")
    stale = _mtime(p)
    # Simulate an external rewrite bumping the mtime past what we recorded.
    p.write_text("new from the outside")
    os.utime(p, ns=(stale + 1000, stale + 1000))
    src = ChangedFilesContextSource(lambda: {str(p): stale})
    out = run(src.render())
    assert out is not None
    assert "Files changed on disk" in out
    assert "a.py" in out


def test_change_reported_once_then_silent(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("old")
    stale = _mtime(p)
    p.write_text("new")
    os.utime(p, ns=(stale + 1000, stale + 1000))
    # The recorded read-state stays at the stale mtime (agent hasn't re-read).
    src = ChangedFilesContextSource(lambda: {str(p): stale})
    assert run(src.render()) is not None  # first turn: reported
    assert run(src.render()) is None  # second turn: already announced, quiet


def test_second_external_change_reannounced(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("old")
    stale = _mtime(p)
    state = {str(p): stale}
    src = ChangedFilesContextSource(lambda: state)

    p.write_text("change one")
    os.utime(p, ns=(stale + 1000, stale + 1000))
    assert run(src.render()) is not None

    # A further external change past the just-reported revision re-announces.
    p.write_text("change two")
    os.utime(p, ns=(stale + 2000, stale + 2000))
    assert run(src.render()) is not None


def test_agent_reread_clears_the_flag(tmp_path):
    # After the agent re-reads, the recorded mtime catches up to disk → silent.
    p = tmp_path / "a.py"
    p.write_text("old")
    stale = _mtime(p)
    p.write_text("new")
    os.utime(p, ns=(stale + 1000, stale + 1000))
    current = _mtime(p)
    # Read-state now reflects the fresh read.
    src = ChangedFilesContextSource(lambda: {str(p): current})
    assert run(src.render()) is None


def test_relative_display_uses_cwd(tmp_path):
    sub = tmp_path / "pkg"
    sub.mkdir()
    p = sub / "a.py"
    p.write_text("old")
    stale = _mtime(p)
    p.write_text("new")
    os.utime(p, ns=(stale + 1000, stale + 1000))
    src = ChangedFilesContextSource(lambda: {str(p): stale})
    out = run(src.render(cwd=str(tmp_path)))
    assert out is not None
    assert os.path.join("pkg", "a.py") in out
