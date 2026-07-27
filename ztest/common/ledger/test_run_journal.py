#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for :mod:`mote.runtime.ledger.run_journal`.

Exercises the run-level step journal on top of the generic append-only base:
started/completed/failed lifecycle, frontier (``unresolved``), self-anchored
think ``seq`` that stays stable across a journal rebuild (and never collides with
an already-committed round), reap-on-durable, legacy ``EffectRecord`` line
folding in (fail-closed EXTERNAL), the torn-line skip, and per-session isolation.
"""
from __future__ import annotations

import json

import pytest

from mote.runtime.ledger import COMPLETED, FAILED, KIND_THINK, KIND_TOOL, STARTED, RunJournal, StepRecord
from mote.runtime.ledger.run_journal import JOURNAL_FILE_NAME
from mote.runtime.workspace import ArtifactKind, WorkspaceStore


@pytest.fixture
def store(tmp_path):
    return WorkspaceStore(root=tmp_path)


@pytest.fixture
def journal(store):
    return RunJournal("sess-a", store=store)


class TestLifecycle:
    def test_started_then_completed(self, journal):
        journal.record_started("tc-1", KIND_TOOL, "external", name="Bash", tool_call_id="tc-1")
        journal.record_completed("tc-1", payload="done")
        rec = journal.replay("tc-1")
        assert rec is not None
        assert rec.status == COMPLETED
        assert rec.payload == "done"
        assert rec.kind == KIND_TOOL
        # started_at preserved across the terminal write.
        assert rec.started_at > 0
        assert rec.ended_at is not None

    def test_failed_terminal(self, journal):
        journal.record_started("tc-2", KIND_TOOL, "external", name="Web")
        journal.record_failed("tc-2", payload="boom")
        rec = journal.replay("tc-2")
        assert rec.status == FAILED
        assert rec.success is False
        assert rec.payload == "boom"

    def test_replay_missing_is_none(self, journal):
        assert journal.replay("never") is None

    def test_unresolved_lists_started_only(self, journal):
        journal.record_started("a", KIND_TOOL, "external")
        journal.record_started("b", KIND_TOOL, "external")
        journal.record_completed("b", payload="x")
        unresolved = journal.unresolved()
        assert [r.step_id for r in unresolved] == ["a"]


class TestThinkSeq:
    def test_first_seq_is_one(self, journal):
        assert journal.next_think_seq() == 1

    def test_seq_advances_past_recorded_think(self, journal):
        seq = journal.next_think_seq()
        journal.record_started(journal.think_step_id(seq), KIND_THINK, "local", seq=seq)
        assert journal.next_think_seq() == 2

    def test_seq_stable_across_rebuild(self, store):
        first = RunJournal("sess-a", store=store)
        s1 = first.next_think_seq()
        first.record_started(first.think_step_id(s1), KIND_THINK, "local", seq=s1)
        first.record_completed(first.think_step_id(s1), payload="r1")
        s2 = first.next_think_seq()
        first.record_started(first.think_step_id(s2), KIND_THINK, "local", seq=s2)

        # A fresh instance (post-crash rebuild) recomputes the next seq purely
        # from the folded journal — it does not collide with the committed s1/s2.
        second = RunJournal("sess-a", store=store)
        assert second.next_think_seq() == 3

    def test_seq_ignores_non_think_kinds(self, journal):
        # A tool step with a high seq must not inflate the think counter.
        journal.record_started("tc", KIND_TOOL, "external", seq=99)
        assert journal.next_think_seq() == 1

    def test_think_step_id_shape(self, journal):
        assert journal.think_step_id(4) == "think:4"


class TestDurability:
    def test_second_instance_sees_prior_writes(self, store):
        first = RunJournal("sess-a", store=store)
        first.record_started("tc-1", KIND_TOOL, "external", name="Bash")
        first.record_completed("tc-1", payload="ok")
        second = RunJournal("sess-a", store=store)
        rec = second.replay("tc-1")
        assert rec.status == COMPLETED
        assert rec.payload == "ok"

    def test_reap_drops_resolved(self, journal):
        journal.record_started("tc-1", KIND_TOOL, "external")
        journal.record_completed("tc-1", payload="ok")
        journal.reap(["tc-1"])
        assert journal.replay("tc-1") is None

    def test_torn_line_skipped(self, store):
        path = store.space("sess-a", ArtifactKind.LEDGER) / JOURNAL_FILE_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        good = StepRecord(step_id="a", kind=KIND_TOOL, effect="external", status=STARTED).to_json()
        path.write_text(good + "\n" + "{garbled" + "\n", encoding="utf-8")
        journal = RunJournal("sess-a", store=store)
        assert journal.replay("a") is not None
        assert len(journal.records()) == 1


class TestLegacyCompat:
    def test_legacy_effect_record_line_folds_in(self, store):
        """An old EffectRecord line (no step_id/kind/effect) folds in as an
        EXTERNAL tool step keyed on its tool_call_id (fail-closed)."""
        path = store.space("sess-a", ArtifactKind.LEDGER) / JOURNAL_FILE_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        legacy = json.dumps(
            {
                "tool_call_id": "tc-legacy",
                "tool_name": "Bash",
                "status": "completed",
                "started_at": 1.0,
                "ended_at": 2.0,
                "result": "legacy output",
                "success": True,
            }
        )
        path.write_text(legacy + "\n", encoding="utf-8")
        journal = RunJournal("sess-a", store=store)
        rec = journal.replay("tc-legacy")
        assert rec is not None
        assert rec.kind == KIND_TOOL
        assert rec.effect == "external"  # fail-closed default
        assert rec.name == "Bash"
        assert rec.payload == "legacy output"
        assert rec.status == COMPLETED

    def test_to_json_carries_back_compat_aliases(self):
        rec = StepRecord(step_id="tc", kind=KIND_TOOL, effect="external", status=COMPLETED, name="Bash", payload="out")
        d = json.loads(rec.to_json())
        assert d["tool_name"] == "Bash"
        assert d["result"] == "out"


class TestSessionIsolation:
    def test_sessions_do_not_share_records(self, store):
        a = RunJournal("sess-a", store=store)
        b = RunJournal("sess-b", store=store)
        a.record_started("tc-1", KIND_TOOL, "external")
        a.record_completed("tc-1", payload="a-only")
        assert b.replay("tc-1") is None
