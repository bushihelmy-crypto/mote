#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for :mod:`mote.session.hunk_ledger`.

Covers the per-session, durable change-attribution ledger: the thin
:class:`HunkRecord` (geometry + attribution + ``pre_hash``, no duplicated hunk
text), its JSON round-trip, the fold-latest-per-``hunk_id`` reads, the
status-change fold (a fresh append that supersedes the prior record), and
cross-instance durability (a fresh ledger for the same session sees prior
writes) — all inherited from :class:`~mote.common.ledger.AppendOnlyLedger`.
"""
from __future__ import annotations

import json

import pytest

from mote.common.workspace import ArtifactKind, WorkspaceStore
from mote.session.hunk_ledger import (
    ACCEPTED,
    AGENT,
    EXTERNAL,
    LEDGER_FILE_NAME,
    PENDING,
    REJECTED,
    HunkLedger,
    HunkRecord,
)

SESSION = "sess-1"


@pytest.fixture
def store(tmp_path):
    return WorkspaceStore(tmp_path)


@pytest.fixture
def ledger(store):
    return HunkLedger(SESSION, store=store)


def _rec(
    hunk_id: str = "h1",
    *,
    path: str = "a.py",
    tool_call_id: str = "call-1",
    turn_index: int = 1,
    source: str = AGENT,
    old_range: tuple[int, int] = (1, 2),
    new_range: tuple[int, int] = (1, 3),
    pre_hash: str = "deadbeef",
    status: str = PENDING,
) -> HunkRecord:
    return HunkRecord(
        hunk_id=hunk_id,
        path=path,
        session_id=SESSION,
        tool_call_id=tool_call_id,
        turn_index=turn_index,
        source=source,
        old_range=old_range,
        new_range=new_range,
        pre_hash=pre_hash,
        status=status,
    )


class TestRecord:
    def test_defaults(self):
        rec = _rec()
        assert rec.status == PENDING
        assert rec.ts > 0

    def test_is_agent(self):
        assert _rec(source=AGENT).is_agent
        assert not _rec(source=EXTERNAL).is_agent

    def test_is_external(self):
        assert _rec(source=EXTERNAL).is_external
        assert not _rec(source=AGENT).is_external

    def test_json_round_trip(self):
        rec = _rec(status=ACCEPTED, turn_index=7)
        back = HunkRecord.from_dict(json.loads(rec.to_json()))
        assert back == rec
        # ranges survive as int tuples (JSON serializes them as lists)
        assert isinstance(back.old_range, tuple)
        assert back.old_range == (1, 2)
        assert back.new_range == (1, 3)

    def test_from_dict_defaults_missing(self):
        rec = HunkRecord.from_dict({"hunk_id": "h9"})
        assert rec.hunk_id == "h9"
        assert rec.old_range == (0, 0)
        assert rec.new_range == (0, 0)
        assert rec.source == AGENT
        assert rec.status == PENDING

    def test_with_status_copies_new_status(self):
        rec = _rec()
        updated = rec.with_status(ACCEPTED)
        assert updated.status == ACCEPTED
        assert updated.hunk_id == rec.hunk_id
        assert updated.turn_index == rec.turn_index
        assert updated.old_range == rec.old_range
        # original untouched (frozen)
        assert rec.status == PENDING


class TestPath:
    def test_path_under_ledger_space(self, ledger, store):
        expected = store.space(SESSION, ArtifactKind.LEDGER) / LEDGER_FILE_NAME
        assert ledger.path == expected

    def test_no_file_before_first_write(self, ledger):
        assert not ledger.path.exists()


class TestReadsAndWrites:
    def test_record_then_status(self, ledger):
        ledger.record(_rec("h1"))
        rec = ledger.status("h1")
        assert rec is not None
        assert rec.hunk_id == "h1"
        assert rec.status == PENDING

    def test_status_missing_is_none(self, ledger):
        assert ledger.status("nope") is None

    def test_latest_folds_per_hunk_id(self, ledger):
        ledger.record(_rec("h1", turn_index=1))
        ledger.record(_rec("h1", turn_index=2))
        rec = ledger.status("h1")
        assert rec is not None
        assert rec.turn_index == 2
        assert len(ledger.records()) == 1

    def test_pending_only(self, ledger):
        ledger.record(_rec("h1"))
        ledger.record(_rec("h2"))
        ledger.set_status("h2", ACCEPTED)
        pending = ledger.pending()
        assert [r.hunk_id for r in pending] == ["h1"]

    def test_for_turn(self, ledger):
        ledger.record(_rec("h1", turn_index=1))
        ledger.record(_rec("h2", turn_index=2))
        ledger.record(_rec("h3", turn_index=1))
        assert {r.hunk_id for r in ledger.for_turn(1)} == {"h1", "h3"}
        assert {r.hunk_id for r in ledger.for_turn(2)} == {"h2"}

    def test_for_path(self, ledger):
        ledger.record(_rec("h1", path="a.py"))
        ledger.record(_rec("h2", path="b.py"))
        assert {r.hunk_id for r in ledger.for_path("a.py")} == {"h1"}

    def test_set_status_folds(self, ledger):
        ledger.record(_rec("h1"))
        updated = ledger.set_status("h1", REJECTED)
        assert updated is not None
        assert updated.status == REJECTED
        assert ledger.status("h1").status == REJECTED  # type: ignore[union-attr]

    def test_set_status_unknown_returns_none(self, ledger):
        assert ledger.set_status("nope", ACCEPTED) is None


class TestDurability:
    def test_second_instance_sees_prior_writes(self, store):
        first = HunkLedger(SESSION, store=store)
        first.record(_rec("h1", turn_index=3))
        first.set_status("h1", ACCEPTED)

        second = HunkLedger(SESSION, store=store)
        rec = second.status("h1")
        assert rec is not None
        assert rec.turn_index == 3
        assert rec.status == ACCEPTED

    def test_append_is_durable_immediately(self, ledger):
        ledger.record(_rec("h1"))
        assert ledger.path.exists()
        assert ledger.path.read_text(encoding="utf-8").strip() != ""

    def test_reap_drops_resolved(self, store):
        first = HunkLedger(SESSION, store=store)
        first.record(_rec("h1"))
        first.record(_rec("h2"))
        first.reap(["h1"])

        second = HunkLedger(SESSION, store=store)
        assert second.status("h1") is None
        assert second.status("h2") is not None
