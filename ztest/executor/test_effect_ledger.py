#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Unit tests for :mod:`mote.runtime.tools.effect_ledger`.

Covers the durable started/completed/failed ledger for EXTERNAL tool calls:
fold-latest-per-id on load, ``unresolved`` surfacing only ``started`` records,
cross-instance durability (a fresh ledger for the same session sees prior
writes), the ``reap`` compaction rewrite, and the graceful skip of torn/garbled
lines.
"""
from __future__ import annotations

import json

import pytest

from mote.runtime.tools.effect_ledger import COMPLETED, FAILED, LEDGER_FILE_NAME, STARTED, EffectLedger, EffectRecord
from mote.runtime.workspace import ArtifactKind, WorkspaceStore

SESSION = "sess-1"


@pytest.fixture
def store(tmp_path):
    return WorkspaceStore(tmp_path)


@pytest.fixture
def ledger(store):
    return EffectLedger(SESSION, store=store)


class TestPath:
    def test_path_under_ledger_space(self, ledger, store):
        expected = store.space(SESSION, ArtifactKind.LEDGER) / LEDGER_FILE_NAME
        assert ledger.path == expected

    def test_no_file_before_first_write(self, ledger):
        assert not ledger.path.exists()


class TestLifecycle:
    def test_mark_started_records_started(self, ledger):
        ledger.mark_started("c1", "Bash")
        rec = ledger.status("c1")
        assert rec is not None
        assert rec.status == STARTED
        assert rec.tool_name == "Bash"
        assert rec.ended_at is None

    def test_mark_started_is_durable(self, ledger):
        # fsync'd append lands on disk immediately (before the body runs).
        ledger.mark_started("c1", "Bash")
        assert ledger.path.exists()
        assert ledger.path.read_text(encoding="utf-8").strip() != ""

    def test_mark_completed_terminal(self, ledger):
        ledger.mark_started("c1", "Bash")
        ledger.mark_completed("c1", "Bash", result="ok")
        rec = ledger.status("c1")
        assert rec is not None
        assert rec.status == COMPLETED
        assert rec.success is True
        assert rec.result == "ok"
        assert rec.ended_at is not None

    def test_mark_failed_terminal(self, ledger):
        ledger.mark_started("c1", "Bash")
        ledger.mark_failed("c1", "Bash", result="boom")
        rec = ledger.status("c1")
        assert rec is not None
        assert rec.status == FAILED
        assert rec.success is False
        assert rec.result == "boom"

    def test_status_unknown_id_is_none(self, ledger):
        assert ledger.status("never-seen") is None


class TestUnresolved:
    def test_unresolved_lists_only_started(self, ledger):
        ledger.mark_started("open", "Bash")
        ledger.mark_started("done", "Bash")
        ledger.mark_completed("done", "Bash", result="ok")
        ledger.mark_started("failed", "Bash")
        ledger.mark_failed("failed", "Bash")

        unresolved = {r.tool_call_id for r in ledger.unresolved()}
        assert unresolved == {"open"}

    def test_no_unresolved_when_all_terminal(self, ledger):
        ledger.mark_started("c1", "Bash")
        ledger.mark_completed("c1", "Bash")
        assert ledger.unresolved() == []


class TestDurabilityAcrossInstances:
    def test_fresh_instance_sees_prior_state(self, store):
        first = EffectLedger(SESSION, store=store)
        first.mark_started("c1", "Bash")
        first.mark_started("c2", "WebBrowser")
        first.mark_completed("c2", "WebBrowser", result="done")

        # A brand-new instance (e.g. the resume reconciler in a new process).
        second = EffectLedger(SESSION, store=store)
        assert {r.tool_call_id for r in second.unresolved()} == {"c1"}
        c1 = second.status("c1")
        assert c1 is not None and c1.status == STARTED
        c2 = second.status("c2")
        assert c2 is not None and c2.status == COMPLETED and c2.result == "done"

    def test_terminal_carries_started_at_forward(self, store):
        first = EffectLedger(SESSION, store=store)
        first.mark_started("c1", "Bash")
        started_at = first.status("c1").started_at  # type: ignore[union-attr]

        second = EffectLedger(SESSION, store=store)
        second.mark_completed("c1", "Bash", result="ok")
        rec = second.status("c1")
        assert rec is not None
        assert rec.started_at == started_at
        assert rec.ended_at is not None and rec.ended_at >= started_at


class TestReap:
    def test_reap_removes_and_rewrites(self, ledger):
        ledger.mark_started("c1", "Bash")
        ledger.mark_completed("c1", "Bash")
        ledger.mark_started("c2", "Bash")
        ledger.mark_completed("c2", "Bash")

        ledger.reap(["c1"])
        assert ledger.status("c1") is None
        assert ledger.status("c2") is not None

        # The rewrite dropped c1 from disk too (a fresh instance agrees).
        reloaded = EffectLedger(SESSION, store=ledger._store)  # type: ignore[attr-defined]
        assert reloaded.status("c1") is None
        assert reloaded.status("c2") is not None

    def test_reap_empty_is_noop(self, ledger):
        ledger.mark_started("c1", "Bash")
        ledger.reap([])
        assert ledger.status("c1") is not None


class TestLoadRobustness:
    def test_fold_keeps_latest_record_per_id(self, store):
        led = EffectLedger(SESSION, store=store)
        led.mark_started("c1", "Bash")
        led.mark_completed("c1", "Bash", result="final")
        # Two physical lines, one folded record.
        lines = [ln for ln in led.path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(lines) == 2
        reloaded = EffectLedger(SESSION, store=store)
        assert reloaded.status("c1").status == COMPLETED  # type: ignore[union-attr]

    def test_torn_line_is_skipped(self, store):
        led = EffectLedger(SESSION, store=store)
        led.mark_started("c1", "Bash")
        # Simulate a torn/garbled trailing line from a crash mid-append.
        with open(led.path, "a", encoding="utf-8") as f:
            f.write('{"tool_call_id": "c2", "st')  # truncated JSON, no newline
        reloaded = EffectLedger(SESSION, store=store)
        assert reloaded.status("c1") is not None
        assert reloaded.status("c2") is None

    def test_blank_lines_ignored(self, store):
        led = EffectLedger(SESSION, store=store)
        led.mark_started("c1", "Bash")
        with open(led.path, "a", encoding="utf-8") as f:
            f.write("\n\n  \n")
        reloaded = EffectLedger(SESSION, store=store)
        assert reloaded.status("c1") is not None


class TestEffectRecordSerialization:
    def test_round_trip(self):
        rec = EffectRecord(
            tool_call_id="c1",
            tool_name="Bash",
            status=COMPLETED,
            started_at=1.0,
            ended_at=2.0,
            result="r",
            success=True,
        )
        back = EffectRecord.from_dict(json.loads(rec.to_json()))
        assert back == rec

    def test_from_dict_defaults(self):
        rec = EffectRecord.from_dict({"tool_call_id": "c1"})
        assert rec.tool_call_id == "c1"
        assert rec.tool_name == ""
        assert rec.status == STARTED
        assert rec.ended_at is None
        assert rec.success is True
