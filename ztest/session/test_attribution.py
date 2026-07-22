#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for :mod:`mote.session.attribution` — the hunk review read-API.

Exercises the grouped queries (by turn / file / source), the whole-session
tally, and body rehydration (OLD side sliced from the before-image blob at
``old_range``; NEW side sliced from the live file at ``new_range``) against a
real :class:`HunkLedger` + an in-memory sha256 blob store.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

import pytest

from mote.common.text.hunks import split_hunks
from mote.common.workspace import WorkspaceStore
from mote.session.attribution import HunkAttribution
from mote.session.hunk_ledger import ACCEPTED, AGENT, EXTERNAL, PENDING, REJECTED, HunkLedger, HunkRecord

SESSION = "sess-attr-q"


class _MemBlobs:
    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    def put(self, content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        self._store[digest] = content
        return digest

    def get(self, digest: str) -> Optional[bytes]:
        return self._store.get(digest)


class _Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.blobs = _MemBlobs()
        self.ledger = HunkLedger(SESSION, store=WorkspaceStore(tmp_path))
        self.attr = HunkAttribution(self.ledger, self.blobs)

    def write(self, name: str, text: str) -> str:
        p = self.tmp_path / name
        p.write_text(text, encoding="utf-8")
        return str(p)

    def add(
        self,
        *,
        hunk_id: str,
        path: str,
        old: str,
        current: str,
        index: int = 0,
        turn_index: int = 1,
        source: str = AGENT,
        status: str = PENDING,
    ) -> HunkRecord:
        pre_hash = self.blobs.put(old.encode("utf-8"))
        hunk = split_hunks(old, current)[index]
        rec = HunkRecord(
            hunk_id=hunk_id,
            path=path,
            session_id=SESSION,
            tool_call_id="c",
            turn_index=turn_index,
            source=source,
            old_range=(hunk.old_start, hunk.old_count),
            new_range=(hunk.new_start, hunk.new_count),
            pre_hash=pre_hash,
            status=status,
        )
        self.ledger.record(rec)
        return rec


@pytest.fixture
def h(tmp_path):
    return _Harness(tmp_path)


class TestRehydration:
    def test_old_and_new_text_derived(self, h):
        old = "line1\nline2\nline3\n"
        current = "line1\nCHANGED\nline3\n"
        path = h.write("f.py", current)
        h.add(hunk_id="h1", path=path, old=old, current=current)

        [view] = h.attr.hunks_for_file(path)
        assert view.old_text == "line2\n"
        assert view.new_text == "CHANGED\n"

    def test_pure_insertion_has_empty_old(self, h):
        old = "a\nb\n"
        current = "a\nNEW\nb\n"
        path = h.write("f.py", current)
        h.add(hunk_id="h1", path=path, old=old, current=current)
        [view] = h.attr.hunks_for_file(path)
        assert view.old_text == ""
        assert view.new_text == "NEW\n"

    def test_pure_deletion_has_empty_new(self, h):
        old = "a\nGONE\nb\n"
        current = "a\nb\n"
        path = h.write("f.py", current)
        h.add(hunk_id="h1", path=path, old=old, current=current)
        [view] = h.attr.hunks_for_file(path)
        assert view.old_text == "GONE\n"
        assert view.new_text == ""

    def test_missing_blob_yields_empty_old(self, h):
        # A record whose pre_hash points at nothing → empty old side, no raise.
        path = h.write("f.py", "x\n")
        rec = HunkRecord(
            hunk_id="h1",
            path=path,
            session_id=SESSION,
            tool_call_id="c",
            turn_index=1,
            source=AGENT,
            old_range=(1, 1),
            new_range=(1, 1),
            pre_hash="0" * 64,
        )
        h.ledger.record(rec)
        [view] = h.attr.all_hunks()
        assert view.old_text == ""
        assert view.new_text == "x\n"

    def test_unreadable_file_yields_empty_new(self, h):
        old = "a\nb\n"
        current = "a\nB\n"
        missing = str(h.tmp_path / "gone.py")
        h.add(hunk_id="h1", path=missing, old=old, current=current)
        [view] = h.attr.all_hunks()
        assert view.old_text == "b\n"
        assert view.new_text == ""  # file does not exist → empty


class TestGrouping:
    def _seed(self, h):
        pa = h.write("a.py", "A\n")
        pb = h.write("b.py", "B\n")
        h.add(hunk_id="ha", path=pa, old="a\n", current="A\n", turn_index=1, source=AGENT)
        h.add(hunk_id="hb", path=pb, old="b\n", current="B\n", turn_index=2, source=EXTERNAL)
        return pa, pb

    def test_hunks_for_turn(self, h):
        self._seed(h)
        assert [v.hunk_id for v in h.attr.hunks_for_turn(1)] == ["ha"]
        assert [v.hunk_id for v in h.attr.hunks_for_turn(2)] == ["hb"]

    def test_hunks_for_file(self, h):
        pa, _ = self._seed(h)
        views = h.attr.hunks_for_file(pa)
        assert [v.hunk_id for v in views] == ["ha"]
        assert views[0].path == pa

    def test_hunks_for_file_sorted_top_first(self, h):
        old = "a\nb\nc\nd\ne\n"
        current = "A\nb\nc\nd\nE\n"
        path = h.write("f.py", current)
        hunks = split_hunks(old, current)
        # Insert in REVERSE order to prove the query sorts by new_range start.
        for i in reversed(range(len(hunks))):
            hk = hunks[i]
            h.ledger.record(
                HunkRecord(
                    hunk_id=f"h{i}",
                    path=path,
                    session_id=SESSION,
                    tool_call_id="c",
                    turn_index=1,
                    source=AGENT,
                    old_range=(hk.old_start, hk.old_count),
                    new_range=(hk.new_start, hk.new_count),
                    pre_hash=h.blobs.put(old.encode("utf-8")),
                )
            )
        starts = [v.record.new_range[0] for v in h.attr.hunks_for_file(path)]
        assert starts == sorted(starts)

    def test_hunks_by_source(self, h):
        self._seed(h)
        assert [v.hunk_id for v in h.attr.hunks_by_source(AGENT)] == ["ha"]
        assert [v.hunk_id for v in h.attr.hunks_by_source(EXTERNAL)] == ["hb"]

    def test_pending_only(self, h):
        self._seed(h)
        h.ledger.set_status("hb", ACCEPTED)
        assert [v.hunk_id for v in h.attr.pending()] == ["ha"]

    def test_view_passthroughs(self, h):
        pa, _ = self._seed(h)
        v = h.attr.hunks_for_turn(1)[0]
        assert v.hunk_id == "ha"
        assert v.source == AGENT
        assert v.status == PENDING
        assert v.turn_index == 1
        assert v.is_pending


class TestSummary:
    def test_counts_and_files(self, h):
        pa = h.write("a.py", "A\n")
        pb = h.write("b.py", "B\n")
        h.add(hunk_id="ha", path=pa, old="a\n", current="A\n", source=AGENT)
        h.add(hunk_id="hb", path=pb, old="b\n", current="B\n", source=EXTERNAL)
        h.add(hunk_id="hc", path=pa, old="a\n", current="A\n", index=0, source=AGENT)
        h.ledger.set_status("ha", ACCEPTED)
        h.ledger.set_status("hb", REJECTED)

        s = h.attr.session_summary()
        assert s.total == 3
        assert s.pending == 1  # hc
        assert s.accepted == 1  # ha
        assert s.rejected == 1  # hb
        assert s.by_source == {AGENT: 2, EXTERNAL: 1}
        assert s.by_status == {ACCEPTED: 1, REJECTED: 1, PENDING: 1}
        assert s.by_path[pa] == 2
        assert s.by_path[pb] == 1
        assert s.files == sorted([pa, pb])

    def test_empty_summary(self, h):
        s = h.attr.session_summary()
        assert s.total == 0
        assert s.pending == 0
        assert s.files == []
        assert s.by_source == {}
