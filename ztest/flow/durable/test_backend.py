#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``mote.runtime.durable.backend`` — the Tier-1 JSONL durable backend.

``JsonlBackend`` memoizes a run's replay-safe steps in the shared
:class:`RunJournal`: a completed step's payload is replayed WITHOUT re-running
``execute`` (skip the re-pay), while any other prior state re-runs it. This is
the always-on, zero-dependency engine both durability tiers share via the
:class:`DurableBackend` protocol.
"""
from __future__ import annotations

import pytest

from mote.runtime.durable import DurableBackend, JsonlBackend
from mote.runtime.ledger import COMPLETED, FAILED, KIND_THINK, STARTED, RunJournal
from mote.runtime.workspace import WorkspaceStore


def _journal(tmp_path, session_id="sess") -> RunJournal:
    return RunJournal(session_id, store=WorkspaceStore(root=str(tmp_path)))


def test_backend_satisfies_protocol(tmp_path):
    backend = JsonlBackend(_journal(tmp_path))
    assert isinstance(backend, DurableBackend)


@pytest.mark.asyncio
async def test_first_run_executes_and_records_completed(tmp_path):
    journal = _journal(tmp_path)
    backend = JsonlBackend(journal)
    calls = []

    async def execute() -> str:
        calls.append(1)
        return "the result"

    out = await backend.run_step("think:1", KIND_THINK, "pure", execute, seq=1)

    assert out == "the result"
    assert calls == [1]  # ran exactly once
    rec = journal.replay("think:1")
    assert rec is not None and rec.status == COMPLETED and rec.payload == "the result"


@pytest.mark.asyncio
async def test_completed_step_replays_without_executing(tmp_path):
    journal = _journal(tmp_path)
    backend = JsonlBackend(journal)

    async def execute_first() -> str:
        return "recorded"

    await backend.run_step("think:1", KIND_THINK, "pure", execute_first, seq=1)

    # Rebuild the backend in a fresh process (post-crash) — the folded journal
    # still carries the completed record.
    rebuilt = JsonlBackend(_journal(tmp_path))
    ran = []

    async def execute_again() -> str:
        ran.append(1)
        return "should NOT be used"

    out = await rebuilt.run_step("think:1", KIND_THINK, "pure", execute_again, seq=1)

    assert out == "recorded"  # replayed, not re-run
    assert ran == []  # execute skipped entirely


@pytest.mark.asyncio
async def test_started_record_reruns(tmp_path):
    # A crash mid-step leaves a ``started`` record; for a replay-safe step
    # re-running is safe, so run_step re-executes (only ``completed`` skips).
    journal = _journal(tmp_path)
    journal.record_started("think:1", KIND_THINK, "pure", seq=1)
    backend = JsonlBackend(journal)
    ran = []

    async def execute() -> str:
        ran.append(1)
        return "fresh"

    out = await backend.run_step("think:1", KIND_THINK, "pure", execute, seq=1)

    assert out == "fresh" and ran == [1]


@pytest.mark.asyncio
async def test_failure_records_failed_and_reraises(tmp_path):
    journal = _journal(tmp_path)
    backend = JsonlBackend(journal)

    async def execute() -> str:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await backend.run_step("think:1", KIND_THINK, "pure", execute, seq=1)

    rec = journal.replay("think:1")
    assert rec is not None and rec.status == FAILED and "boom" in (rec.payload or "")


@pytest.mark.asyncio
async def test_journal_property_is_the_injected_instance(tmp_path):
    journal = _journal(tmp_path)
    assert JsonlBackend(journal).journal is journal
