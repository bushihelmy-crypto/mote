#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for :class:`mote.session.subscribers.HunkSubscriber`.

The subscriber derives *agent* change hunks at the tool-settle point: it splits
each successful result's ``FileChange`` old→new into hunks and appends one
``source=agent`` :class:`HunkRecord` per hunk to the session's ledger, stamped
with the live turn index. Covers derivation, turn stamping, the skip conditions
(no changes / failed call / non-mutating result), whole-file create/delete, the
``pre_hash`` = sha256(old) contract, id stability (idempotent re-handle), and the
``enabled`` gate.
"""
from __future__ import annotations

from typing import Optional

import pytest

from mote.common.events.types import PostToolUseEvent
from mote.common.text.hashing import content_hash
from mote.common.workspace import WorkspaceStore
from mote.executor.tool_result import FileChange
from mote.session.hunk_ledger import AGENT, HunkLedger
from mote.session.subscribers import HunkSubscriber

SESSION = "sess-1"


class _MemBlobs:
    """In-memory sha256 content-addressed blob store (sync put/get)."""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    def put(self, content: bytes) -> str:
        digest = content_hash(content.decode("utf-8"))
        self._store[digest] = content
        return digest

    def get(self, digest):
        return self._store.get(digest)


@pytest.fixture
def store(tmp_path):
    return WorkspaceStore(tmp_path)


@pytest.fixture
def ledger(store):
    return HunkLedger(SESSION, store=store)


def _sub(ledger, turn=1, enabled=True, blobs=None):
    return HunkSubscriber(ledger, lambda: turn, SESSION, blobs or _MemBlobs(), enabled=enabled)


def _event(changes, *, success=True, tool_use_id: Optional[str] = "call-1"):
    return PostToolUseEvent(
        tool_name="Edit",
        tool_use_id=tool_use_id,
        success=success,
        file_changes=changes,
    )


async def _handle(sub, event):
    await sub.handle(event)


class TestDerivation:
    @pytest.mark.asyncio
    async def test_records_agent_hunk(self, ledger):
        sub = _sub(ledger, turn=3)
        await _handle(sub, _event([FileChange(path="a.py", old="x\n", new="y\n")]))
        recs = ledger.records()
        assert len(recs) == 1
        rec = recs[0]
        assert rec.source == AGENT
        assert rec.is_agent
        assert rec.path == "a.py"
        assert rec.turn_index == 3
        assert rec.tool_call_id == "call-1"

    @pytest.mark.asyncio
    async def test_pre_hash_is_sha256_of_old(self, ledger):
        sub = _sub(ledger)
        old = "line1\nline2\n"
        await _handle(sub, _event([FileChange(path="a.py", old=old, new="line1\nCHANGED\n")]))
        rec = ledger.records()[0]
        assert rec.pre_hash == content_hash(old)

    @pytest.mark.asyncio
    async def test_multiple_hunks_multiple_records(self, ledger):
        # Two changed blocks separated by an unchanged line → two hunks.
        old = "a\nb\nc\nd\ne\n"
        new = "A\nb\nc\nd\nE\n"
        sub = _sub(ledger)
        await _handle(sub, _event([FileChange(path="a.py", old=old, new=new)]))
        assert len(ledger.records()) == 2

    @pytest.mark.asyncio
    async def test_multiple_file_changes(self, ledger):
        sub = _sub(ledger)
        await _handle(
            sub,
            _event(
                [
                    FileChange(path="a.py", old="1\n", new="2\n"),
                    FileChange(path="b.py", old="3\n", new="4\n"),
                ]
            ),
        )
        assert {r.path for r in ledger.records()} == {"a.py", "b.py"}

    @pytest.mark.asyncio
    async def test_file_creation_one_hunk(self, ledger):
        sub = _sub(ledger)
        await _handle(sub, _event([FileChange(path="new.py", old="", new="hello\nworld\n")]))
        recs = ledger.records()
        assert len(recs) == 1
        # Pure insertion: old range count is 0.
        assert recs[0].old_range[1] == 0

    @pytest.mark.asyncio
    async def test_file_deletion_one_hunk(self, ledger):
        sub = _sub(ledger)
        await _handle(sub, _event([FileChange(path="gone.py", old="hello\nworld\n", new="")]))
        recs = ledger.records()
        assert len(recs) == 1
        # Pure deletion: new range count is 0.
        assert recs[0].new_range[1] == 0


class TestSkips:
    @pytest.mark.asyncio
    async def test_no_change_no_record(self, ledger):
        sub = _sub(ledger)
        await _handle(sub, _event([FileChange(path="a.py", old="same\n", new="same\n")]))
        assert ledger.records() == []

    @pytest.mark.asyncio
    async def test_failed_call_skipped(self, ledger):
        sub = _sub(ledger)
        await _handle(sub, _event([FileChange(path="a.py", old="x\n", new="y\n")], success=False))
        assert ledger.records() == []

    @pytest.mark.asyncio
    async def test_no_file_changes_skipped(self, ledger):
        sub = _sub(ledger)
        await _handle(sub, _event([]))
        assert ledger.records() == []

    @pytest.mark.asyncio
    async def test_non_post_tool_event_ignored(self, ledger):
        sub = _sub(ledger)
        await _handle(sub, object())  # arbitrary non-event
        assert ledger.records() == []

    @pytest.mark.asyncio
    async def test_disabled_records_nothing(self, ledger):
        sub = _sub(ledger, enabled=False)
        await _handle(sub, _event([FileChange(path="a.py", old="x\n", new="y\n")]))
        assert ledger.records() == []


class TestIdStability:
    @pytest.mark.asyncio
    async def test_same_call_folds_idempotently(self, ledger):
        # A resume that replays the same tool_use_id must fold onto the same
        # records, not duplicate them.
        sub = _sub(ledger)
        change = [FileChange(path="a.py", old="x\n", new="y\n")]
        await _handle(sub, _event(change, tool_use_id="call-9"))
        await _handle(sub, _event(change, tool_use_id="call-9"))
        assert len(ledger.records()) == 1

    @pytest.mark.asyncio
    async def test_missing_tool_use_id_still_records(self, ledger):
        sub = _sub(ledger)
        await _handle(sub, _event([FileChange(path="a.py", old="x\n", new="y\n")], tool_use_id=None))
        recs = ledger.records()
        assert len(recs) == 1
        assert recs[0].tool_call_id == ""


class TestPreHashBackendNative:
    """The recorded ``pre_hash`` must fetch the before-image back under *any*
    blob backend — the digest is the store's own ``put`` return value, not a
    hard-coded sha256. This is the regression guard for the git-backend bug
    where a hard-coded sha256 ``pre_hash`` never matched the git object id.
    """

    @pytest.mark.asyncio
    async def test_git_backend_pre_hash_round_trips(self, ledger, tmp_path):
        from mote.session.snapshot import GitBlobStore

        blobs = GitBlobStore(tmp_path / "git")
        old = "line1\nline2\n"
        sub = _sub(ledger, blobs=blobs)
        await _handle(sub, _event([FileChange(path="a.py", old=old, new="line1\nCHANGED\n")]))
        rec = ledger.records()[0]
        # The git object id is NOT the sha256 of the content...
        assert rec.pre_hash != content_hash(old)
        # ...yet the before-image is fetchable back under the recorded key.
        assert blobs.get(rec.pre_hash) == old.encode("utf-8")

    @pytest.mark.asyncio
    async def test_blob_backend_pre_hash_round_trips(self, ledger, tmp_path):
        from mote.session.snapshot import BlobStore

        blobs = BlobStore(tmp_path / "blobs")
        old = "a\nb\n"
        sub = _sub(ledger, blobs=blobs)
        await _handle(sub, _event([FileChange(path="a.py", old=old, new="a\nZ\n")]))
        rec = ledger.records()[0]
        assert rec.pre_hash == content_hash(old)  # sha256 backend
