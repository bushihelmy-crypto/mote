#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for :mod:`mote.session.hunk_ops` — accept / reject / undo.

Exercises the coordination engine against a real :class:`HunkLedger`, a
lightweight in-memory sha256 blob store (mirroring
:class:`~mote.session.snapshot.BlobStore`'s digest contract without the async
DiskWriter), and captured baseline/read-state callbacks. Records are built from
the SAME :func:`~mote.common.text.hunks.split_hunks` the real subscriber uses,
so their geometry matches live content exactly.

Two anchors under test: a record's ``pre_hash`` (the before-image blob = the
OLD side) vs the live file (the NEW side). ``accept`` keeps disk untouched;
``reject``/``undo`` revert the hunk on disk and shift the remaining pending
records below it.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

import pytest

from mote.common.text.hunks import split_hunks
from mote.common.workspace import WorkspaceStore
from mote.session.hunk_ledger import ACCEPTED, AGENT, PENDING, REJECTED, HunkLedger, HunkRecord
from mote.session.hunk_ops import HunkOps

SESSION = "sess-ops"


class _MemBlobs:
    """In-memory content-addressed blob store (sha256 hex), sync put/get."""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    def put(self, content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        self._store[digest] = content
        return digest

    def get(self, digest: str) -> Optional[bytes]:
        return self._store.get(digest)


class _Harness:
    """Wires HunkOps with real ledger + mem blobs + captured callbacks."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.blobs = _MemBlobs()
        self.ledger = HunkLedger(SESSION, store=WorkspaceStore(tmp_path))
        self.baselines: dict[str, str] = {}  # path -> digest
        self.read_state: dict[str, int] = {}  # path -> mtime_ns
        self.ops = HunkOps(
            self.ledger,
            self.blobs,
            set_baseline=self._set_baseline,
            refresh_read_state=self._refresh_read_state,
        )

    def _set_baseline(self, path: str, digest: str) -> None:
        self.baselines[path] = digest

    def _refresh_read_state(self, path: str) -> None:
        self.read_state[path] = 1

    def write(self, name: str, text: str) -> str:
        p = self.tmp_path / name
        p.write_text(text, encoding="utf-8")
        return str(p)

    def read(self, path: str) -> str:
        return Path(path).read_text(encoding="utf-8")

    def add_record(
        self,
        *,
        hunk_id: str,
        path: str,
        old: str,
        current: str,
        index: int = 0,
        turn_index: int = 1,
        source: str = AGENT,
    ) -> HunkRecord:
        """Record the ``index``-th hunk between ``old`` (blobbed) and ``current``."""
        pre_hash = self.blobs.put(old.encode("utf-8"))
        hunk = split_hunks(old, current)[index]
        rec = HunkRecord(
            hunk_id=hunk_id,
            path=path,
            session_id=SESSION,
            tool_call_id="call-x",
            turn_index=turn_index,
            source=source,
            old_range=(hunk.old_start, hunk.old_count),
            new_range=(hunk.new_start, hunk.new_count),
            pre_hash=pre_hash,
        )
        self.ledger.record(rec)
        return rec


@pytest.fixture
def h(tmp_path):
    return _Harness(tmp_path)


class TestAccept:
    def test_accept_marks_accepted_no_disk_write(self, h):
        old = "line1\nline2\n"
        current = "line1\nCHANGED\n"
        path = h.write("f.py", current)
        h.add_record(hunk_id="h1", path=path, old=old, current=current)

        res = h.ops.accept("h1")
        assert res.ok
        assert res.status == ACCEPTED
        # File on disk is untouched (already what the agent wrote).
        assert h.read(path) == current
        assert h.ledger.status("h1").status == ACCEPTED  # type: ignore[union-attr]

    def test_accept_advances_baseline_to_current(self, h):
        old = "a\n"
        current = "b\n"
        path = h.write("f.py", current)
        h.add_record(hunk_id="h1", path=path, old=old, current=current)

        h.ops.accept("h1")
        # Baseline digest == digest of current on-disk content.
        assert h.baselines[path] == hashlib.sha256(current.encode("utf-8")).hexdigest()

    def test_accept_unknown(self, h):
        res = h.ops.accept("nope")
        assert not res.ok
        assert res.error == "unknown"

    def test_accept_not_pending(self, h):
        old, current = "a\n", "b\n"
        path = h.write("f.py", current)
        h.add_record(hunk_id="h1", path=path, old=old, current=current)
        h.ledger.set_status("h1", ACCEPTED)
        res = h.ops.accept("h1")
        assert not res.ok
        assert res.error == "not pending"


class TestReject:
    def test_reject_reverts_file_to_old(self, h):
        old = "line1\nline2\n"
        current = "line1\nCHANGED\n"
        path = h.write("f.py", current)
        h.add_record(hunk_id="h1", path=path, old=old, current=current)

        res = h.ops.reject("h1")
        assert res.ok
        assert res.status == REJECTED
        # The changed region is restored to its old content.
        assert h.read(path) == old
        assert h.ledger.status("h1").status == REJECTED  # type: ignore[union-attr]

    def test_reject_refreshes_read_state(self, h):
        old, current = "a\n", "b\n"
        path = h.write("f.py", current)
        h.add_record(hunk_id="h1", path=path, old=old, current=current)
        h.ops.reject("h1")
        assert path in h.read_state

    def test_reject_advances_baseline_to_reverted(self, h):
        old, current = "a\n", "b\n"
        path = h.write("f.py", current)
        h.add_record(hunk_id="h1", path=path, old=old, current=current)
        h.ops.reject("h1")
        # Baseline now tracks the reverted (old) content.
        assert h.baselines[path] == hashlib.sha256(old.encode("utf-8")).hexdigest()

    def test_undo_is_reject(self, h):
        assert HunkOps.undo is HunkOps.reject

    def test_reject_drifted_when_no_matching_hunk(self, h):
        old = "line1\nline2\n"
        current = "line1\nCHANGED\n"
        path = h.write("f.py", current)
        h.add_record(hunk_id="h1", path=path, old=old, current=current)
        # A later out-of-band edit reshapes the file so the record no longer
        # matches any live change.
        h.write("f.py", "totally\ndifferent\ncontent\n")
        res = h.ops.reject("h1")
        assert not res.ok
        assert res.error == "drifted"

    def test_reject_unknown(self, h):
        res = h.ops.reject("nope")
        assert not res.ok
        assert res.error == "unknown"

    def test_reject_not_pending(self, h):
        old, current = "a\n", "b\n"
        path = h.write("f.py", current)
        h.add_record(hunk_id="h1", path=path, old=old, current=current)
        h.ledger.set_status("h1", REJECTED)
        res = h.ops.reject("h1")
        assert not res.ok
        assert res.error == "not pending"


class TestRejectShiftsRemaining:
    def test_reject_shifts_lower_pending_new_range(self, h):
        # Two separate change regions in one file. Rejecting the UPPER one
        # (which deletes a line) must shift the LOWER pending record's
        # new_range up by the line delta.
        old = "a\nb\nc\nd\ne\nf\n"
        # Upper hunk: line 2 "b" -> two lines "b1\nb2" (grows current by 1);
        # lower hunk: line 5 "e" -> "E".
        current = "a\nb1\nb2\nc\nd\nE\nf\n"
        path = h.write("f.py", current)
        hunks = split_hunks(old, current)
        assert len(hunks) == 2
        upper, lower = hunks[0], hunks[1]
        h.ledger.record(
            HunkRecord(
                hunk_id="upper",
                path=path,
                session_id=SESSION,
                tool_call_id="c",
                turn_index=1,
                source=AGENT,
                old_range=(upper.old_start, upper.old_count),
                new_range=(upper.new_start, upper.new_count),
                pre_hash=h.blobs.put(old.encode("utf-8")),
            )
        )
        h.ledger.record(
            HunkRecord(
                hunk_id="lower",
                path=path,
                session_id=SESSION,
                tool_call_id="c",
                turn_index=1,
                source=AGENT,
                old_range=(lower.old_start, lower.old_count),
                new_range=(lower.new_start, lower.new_count),
                pre_hash=h.blobs.put(old.encode("utf-8")),
            )
        )
        lower_new_start_before = lower.new_start
        res = h.ops.reject("upper")
        assert res.ok
        # Upper reverted: "b1\nb2" -> "b" removes one current line (delta = -1).
        shifted = h.ledger.status("lower")
        assert shifted is not None
        assert shifted.new_range[0] == lower_new_start_before - 1


class TestBatch:
    def _seed_two_files(self, h):
        old_a, cur_a = "a\n", "A\n"
        old_b, cur_b = "b\n", "B\n"
        pa = h.write("a.py", cur_a)
        pb = h.write("b.py", cur_b)
        h.add_record(hunk_id="ha", path=pa, old=old_a, current=cur_a, turn_index=1)
        h.add_record(hunk_id="hb", path=pb, old=old_b, current=cur_b, turn_index=2)
        return pa, pb, old_a, old_b

    def test_accept_file(self, h):
        pa, pb, *_ = self._seed_two_files(h)
        res = h.ops.accept_file(pa)
        assert res.ok
        assert [r.hunk_id for r in res.results] == ["ha"]
        assert h.ledger.status("ha").status == ACCEPTED  # type: ignore[union-attr]
        assert h.ledger.status("hb").status == PENDING  # type: ignore[union-attr]

    def test_reject_file(self, h):
        pa, pb, old_a, _ = self._seed_two_files(h)
        res = h.ops.reject_file(pa)
        assert res.ok
        assert h.read(pa) == old_a
        assert h.ledger.status("ha").status == REJECTED  # type: ignore[union-attr]

    def test_accept_turn(self, h):
        self._seed_two_files(h)
        res = h.ops.accept_turn(2)
        assert [r.hunk_id for r in res.results] == ["hb"]
        assert h.ledger.status("hb").status == ACCEPTED  # type: ignore[union-attr]
        assert h.ledger.status("ha").status == PENDING  # type: ignore[union-attr]

    def test_reject_turn(self, h):
        pa, pb, _, old_b = self._seed_two_files(h)
        res = h.ops.reject_turn(2)
        assert res.ok
        assert h.read(pb) == old_b
        assert h.ledger.status("hb").status == REJECTED  # type: ignore[union-attr]

    def test_accept_all(self, h):
        self._seed_two_files(h)
        res = h.ops.accept_all()
        assert res.ok
        assert {r.hunk_id for r in res.results} == {"ha", "hb"}
        assert h.ledger.pending() == []

    def test_reject_all(self, h):
        pa, pb, old_a, old_b = self._seed_two_files(h)
        res = h.ops.reject_all()
        assert res.ok
        assert h.read(pa) == old_a
        assert h.read(pb) == old_b
        assert h.ledger.pending() == []

    def test_reject_batch_highest_line_first(self, h):
        # Two hunks in one file; reject_file must process the higher-line hunk
        # first so the lower hunk's live geometry stays valid.
        old = "a\nb\nc\nd\ne\n"
        current = "A\nb\nc\nd\nE\n"
        path = h.write("f.py", current)
        hunks = split_hunks(old, current)
        assert len(hunks) == 2
        for i, hunk in enumerate(hunks):
            h.ledger.record(
                HunkRecord(
                    hunk_id=f"h{i}",
                    path=path,
                    session_id=SESSION,
                    tool_call_id="c",
                    turn_index=1,
                    source=AGENT,
                    old_range=(hunk.old_start, hunk.old_count),
                    new_range=(hunk.new_start, hunk.new_count),
                    pre_hash=h.blobs.put(old.encode("utf-8")),
                )
            )
        res = h.ops.reject_file(path)
        assert res.ok
        # Both reverted -> file fully restored.
        assert h.read(path) == old


class TestBatchEmpty:
    def test_empty_batch_is_vacuously_ok(self, h):
        assert h.ops.accept_all().ok
        assert h.ops.reject_all().ok
        assert h.ops.accept_file("nope.py").results == []
