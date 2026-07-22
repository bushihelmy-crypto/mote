#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for external-change attribution (task #3).

Covers :meth:`RoleCapabilities.record_file_baseline` and
:meth:`RoleCapabilities.attribute_external_change` — the read-before-write
guard's attribution hook. The guard (in ``executor/dependency/_file_base.py``)
calls ``attribute_external_change`` the instant it detects a file changed on
disk since mote last knew it, *before* aborting the write: the delta is diffed
against the recorded baseline (the content mote last wrote/knew) and appended to
the session's :class:`HunkLedger` as ``source=external`` hunks, stamped with the
live turn index and no tool-call id.

We exercise the real capability implementation against a lightweight duck-typed
role (real :class:`RoleStateController`, real :class:`HunkLedger`, an in-memory
blob store) so there is no disk-writer timing to drain.
"""
from __future__ import annotations

import hashlib
import types
from pathlib import Path
from typing import TYPE_CHECKING, Optional, cast

import pytest

from mote.common.workspace import WorkspaceStore
from mote.roles.capabilities import RoleCapabilities
from mote.roles.role_state import RoleState, RoleStateController
from mote.session.hunk_ledger import AGENT, EXTERNAL, HunkLedger

if TYPE_CHECKING:
    from mote.roles.role import Role

SESSION = "sess-attr"


class _MemBlobs:
    """In-memory content-addressed blob store (sha256 hex), sync put/get.

    Mirrors :class:`~mote.session.snapshot.BlobStore`'s digest contract without
    the DiskWriter async write, so a test can put a baseline and immediately
    read it back.
    """

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    def put(self, content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        self._store[digest] = content
        return digest

    def get(self, digest: str) -> Optional[bytes]:
        return self._store.get(digest)


class _FakeRecorder:
    def __init__(self, blobs: _MemBlobs) -> None:
        self.blobs = blobs


class _FakeRole:
    """Duck-typed Role exposing exactly what the attribution capability reads."""

    def __init__(self, ledger: Optional[HunkLedger], *, record_hunks: bool = True) -> None:
        self.hunk_ledger = ledger
        self.state = RoleState(session_id=SESSION)
        self._state_ctl = RoleStateController(self.state)
        self.file_snapshot_recorder = _FakeRecorder(_MemBlobs())
        self.role_schema = types.SimpleNamespace(record_hunks=record_hunks)

    def current_turn_index(self) -> int:
        return self._state_ctl.current_turn_index()


@pytest.fixture
def ledger(tmp_path):
    return HunkLedger(SESSION, store=WorkspaceStore(tmp_path))


@pytest.fixture
def role(ledger):
    return _FakeRole(ledger)


@pytest.fixture
def caps(role):
    return RoleCapabilities(cast("Role", role))


def _write(path: Path, text: str) -> str:
    path.write_text(text, encoding="utf-8")
    return str(path)


class TestAttribution:
    def test_external_change_against_baseline_records_external_hunk(self, tmp_path, role, caps, ledger):
        p = _write(tmp_path / "f.py", "line1\nline2\n")
        caps.record_file_baseline(p)  # mote "wrote" this content
        # An out-of-band edit lands on disk.
        _write(tmp_path / "f.py", "line1\nCHANGED\n")
        caps.attribute_external_change(p)
        recs = ledger.records()
        assert len(recs) == 1
        rec = recs[0]
        assert rec.source == EXTERNAL
        assert rec.is_external
        assert rec.path == p
        assert rec.tool_call_id == ""  # no agent tool call

    def test_turn_index_stamped(self, tmp_path, role, caps, ledger):
        p = _write(tmp_path / "f.py", "a\n")
        caps.record_file_baseline(p)
        role._state_ctl.advance_turn()
        role._state_ctl.advance_turn()  # turn_index == 2
        _write(tmp_path / "f.py", "b\n")
        caps.attribute_external_change(p)
        assert ledger.records()[0].turn_index == 2

    def test_pre_hash_is_baseline_digest(self, tmp_path, role, caps, ledger):
        p = _write(tmp_path / "f.py", "old\n")
        caps.record_file_baseline(p)
        baseline_digest = role._state_ctl.get_file_baseline(p)
        _write(tmp_path / "f.py", "new\n")
        caps.attribute_external_change(p)
        assert ledger.records()[0].pre_hash == baseline_digest

    def test_no_baseline_treats_whole_file_as_external(self, tmp_path, role, caps, ledger):
        # Never baselined (mote only ever read it) => baseline "" => the whole
        # current content reads as an external insertion.
        p = _write(tmp_path / "f.py", "hello\nworld\n")
        caps.attribute_external_change(p)
        recs = ledger.records()
        assert len(recs) == 1
        assert recs[0].source == EXTERNAL
        # Pure insertion against an empty baseline: old range count is 0.
        assert recs[0].old_range[1] == 0

    def test_content_identical_records_nothing(self, tmp_path, role, caps, ledger):
        p = _write(tmp_path / "f.py", "same\n")
        caps.record_file_baseline(p)
        # File "changed" (guard fired on mtime) but content is byte-identical.
        caps.attribute_external_change(p)
        assert ledger.records() == []

    def test_re_baselines_so_second_fire_is_noop(self, tmp_path, role, caps, ledger):
        p = _write(tmp_path / "f.py", "v1\n")
        caps.record_file_baseline(p)
        _write(tmp_path / "f.py", "v2\n")
        caps.attribute_external_change(p)  # records v1->v2 external hunk, re-baselines to v2
        first = len(ledger.records())
        assert first == 1
        # A follow-up guard fire before any further change: content == baseline => no new record.
        caps.attribute_external_change(p)
        assert len(ledger.records()) == first

    def test_multiple_external_edits_accumulate(self, tmp_path, role, caps, ledger):
        p = _write(tmp_path / "f.py", "v1\n")
        caps.record_file_baseline(p)
        _write(tmp_path / "f.py", "v2\n")
        caps.attribute_external_change(p)
        _write(tmp_path / "f.py", "v3\n")
        caps.attribute_external_change(p)
        recs = ledger.records()
        assert len(recs) == 2
        assert all(r.source == EXTERNAL for r in recs)


class TestSkips:
    def test_record_hunks_disabled_no_records(self, tmp_path, ledger):
        role = _FakeRole(ledger, record_hunks=False)
        caps = RoleCapabilities(cast("Role", role))
        p = _write(tmp_path / "f.py", "old\n")
        caps.record_file_baseline(p)  # no-op when disabled
        _write(tmp_path / "f.py", "new\n")
        caps.attribute_external_change(p)  # no-op when disabled
        assert ledger.records() == []
        assert role._state_ctl.get_file_baseline(p) is None

    def test_missing_file_no_raise(self, tmp_path, role, caps, ledger):
        missing = str(tmp_path / "does_not_exist.py")
        caps.record_file_baseline(missing)  # unreadable -> no baseline, no raise
        caps.attribute_external_change(missing)  # unreadable -> no attribution, no raise
        assert ledger.records() == []

    def test_no_ledger_no_raise(self, tmp_path):
        role = _FakeRole.__new__(_FakeRole)
        role.hunk_ledger = None
        role.state = RoleState(session_id=SESSION)
        role._state_ctl = RoleStateController(role.state)
        role.file_snapshot_recorder = _FakeRecorder(_MemBlobs())
        role.role_schema = types.SimpleNamespace(record_hunks=True)
        role.current_turn_index = lambda: 0  # type: ignore[method-assign]
        caps = RoleCapabilities(cast("Role", role))
        p = _write(tmp_path / "f.py", "x\n")
        caps.attribute_external_change(p)  # ledger is None -> no-op, no raise


class TestNotAgentSource:
    def test_external_records_are_not_agent(self, tmp_path, role, caps, ledger):
        p = _write(tmp_path / "f.py", "a\n")
        caps.record_file_baseline(p)
        _write(tmp_path / "f.py", "b\n")
        caps.attribute_external_change(p)
        rec = ledger.records()[0]
        assert not rec.is_agent
        assert rec.source != AGENT
