#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the Tier-2 Temporal backend's non-server logic (B1/B4a).

These exercise the parts that need NO running Temporal server: the
``DurableBackend`` protocol conformance, the INLINE (outside-a-workflow) run_step
path (records to the shared journal exactly like the JSONL tier), the
``ActivityConfig``→``RetryPolicy`` mapping, and the process-local step-handler
registry. The end-to-end workflow / activity-memoization behaviour (which needs a
test server) lives in ``test_workflow.py``.

All gated on ``temporalio`` being importable — the whole file skips when the
``[temporal]`` extra is absent, so the core test env (no temporalio) stays green.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

pytest.importorskip("temporalio")

from mote.contracts.config.tool import ActivityConfig, TemporalConfig
from mote.runtime.durable import DurableBackend
from mote.runtime.durable.temporal import TemporalBackend
from mote.runtime.durable.temporal._activities import StepHandlerRegistry
from mote.runtime.durable.temporal._backend import _activity_kwargs, _retry_policy
from mote.runtime.ledger import COMPLETED, FAILED, KIND_THINK, KIND_TIMER, KIND_TOOL, RunJournal
from mote.runtime.session.workspace import SessionWorkspace


def _journal(tmp_path, session_id="sess") -> RunJournal:
    return RunJournal(session_id, store=SessionWorkspace(root=str(tmp_path)))


def _backend(tmp_path) -> TemporalBackend:
    return TemporalBackend(TemporalConfig(), _journal(tmp_path))


def test_backend_satisfies_protocol(tmp_path):
    backend = _backend(tmp_path)
    assert isinstance(backend, DurableBackend)


def test_journal_is_the_shared_instance(tmp_path):
    journal = _journal(tmp_path)
    backend = TemporalBackend(TemporalConfig(), journal)
    assert backend.journal is journal


def test_exposes_exactly_one_activity(tmp_path):
    # One generic run_step activity handles every step KIND (kind is data, not a
    # separate code path) — mirrors the JSONL tier's single run_step.
    backend = _backend(tmp_path)
    assert len(backend.temporal_activities) == 1


@pytest.mark.asyncio
async def test_inline_first_run_executes_and_records_completed(tmp_path):
    journal = _journal(tmp_path)
    backend = TemporalBackend(TemporalConfig(), journal)
    calls = []

    async def execute() -> str:
        calls.append(1)
        return "the result"

    out = await backend.run_step("think:1", KIND_THINK, "pure", execute, seq=1)
    assert out == "the result"
    assert calls == [1]
    rec = journal.replay("think:1")
    assert rec is not None
    assert rec.status == COMPLETED
    assert rec.payload == "the result"


@pytest.mark.asyncio
async def test_inline_completed_replays_without_executing(tmp_path):
    journal = _journal(tmp_path)
    backend = TemporalBackend(TemporalConfig(), journal)
    calls = []

    async def execute() -> str:
        calls.append(1)
        return "first"

    await backend.run_step("think:1", KIND_THINK, "pure", execute, seq=1)
    # A second call (a resume after the step finished) short-circuits on the
    # completed record: replay the payload, do NOT re-run.
    out = await backend.run_step("think:1", KIND_THINK, "pure", execute, seq=1)
    assert out == "first"
    assert calls == [1]


@pytest.mark.asyncio
async def test_inline_failure_records_failed_and_reraises(tmp_path):
    journal = _journal(tmp_path)
    backend = TemporalBackend(TemporalConfig(), journal)

    async def boom() -> str:
        raise ValueError("kaboom")

    with pytest.raises(ValueError, match="kaboom"):
        await backend.run_step("tool:1", KIND_TOOL, "external", boom)
    rec = journal.replay("tool:1")
    assert rec is not None
    assert rec.status == FAILED
    assert "kaboom" in (rec.payload or "")


@pytest.mark.asyncio
async def test_inline_carries_identity_onto_record(tmp_path):
    journal = _journal(tmp_path)
    backend = TemporalBackend(TemporalConfig(), journal)

    async def execute() -> str:
        return "ok"

    await backend.run_step("tool:call-abc", KIND_TOOL, "external", execute, name="Bash", tool_call_id="call-abc")
    rec = journal.replay("tool:call-abc")
    assert rec is not None
    assert rec.kind == KIND_TOOL
    assert rec.effect == "external"
    assert rec.name == "Bash"
    assert rec.tool_call_id == "call-abc"


# ---- ActivityConfig -> RetryPolicy mapping --------------------------------


def test_retry_policy_maps_defaults():
    from temporalio.common import RetryPolicy

    policy = _retry_policy(ActivityConfig())
    assert isinstance(policy, RetryPolicy)
    # max_retry_attempts=0 (mote default) == unbounded, passes straight through.
    assert policy.maximum_attempts == 0
    assert policy.initial_interval == timedelta(seconds=1.0)
    assert policy.backoff_coefficient == 2.0
    assert policy.maximum_interval is None
    assert policy.non_retryable_error_types == []


def test_retry_policy_maps_explicit_values():
    cfg = ActivityConfig(
        max_retry_attempts=5,
        initial_retry_interval_seconds=2.5,
        retry_backoff_coefficient=3.0,
        max_retry_interval_seconds=60.0,
        non_retryable_error_types=["ToolError", "UserError"],
    )
    policy = _retry_policy(cfg)
    assert policy.maximum_attempts == 5
    assert policy.initial_interval == timedelta(seconds=2.5)
    assert policy.backoff_coefficient == 3.0
    assert policy.maximum_interval == timedelta(seconds=60.0)
    assert policy.non_retryable_error_types == ["ToolError", "UserError"]


def test_activity_kwargs_defaults_start_to_close_timeout():
    # Temporal requires a start_to_close_timeout; when the seam leaves it unset
    # we fall back to a generous default (an LLM turn can run for minutes).
    kwargs = _activity_kwargs(ActivityConfig())
    assert kwargs["start_to_close_timeout"] == timedelta(seconds=600.0)


def test_activity_kwargs_honours_explicit_timeout():
    kwargs = _activity_kwargs(ActivityConfig(start_to_close_timeout_seconds=30.0))
    assert kwargs["start_to_close_timeout"] == timedelta(seconds=30.0)


def test_seam_config_dispatches_by_kind(tmp_path):
    cfg = TemporalConfig(
        think_activity=ActivityConfig(max_retry_attempts=1),
        timer_activity=ActivityConfig(max_retry_attempts=2),
        tool_activity=ActivityConfig(max_retry_attempts=3),
    )
    backend = TemporalBackend(cfg, _journal(tmp_path))
    assert backend._seam_config(KIND_THINK).max_retry_attempts == 1
    assert backend._seam_config(KIND_TIMER).max_retry_attempts == 2
    assert backend._seam_config(KIND_TOOL).max_retry_attempts == 3
    # Unknown kind falls back to the tool policy (never crashes).
    assert backend._seam_config("mystery").max_retry_attempts == 3


# ---- StepHandlerRegistry --------------------------------------------------


def test_registry_pop_returns_and_removes():
    reg = StepHandlerRegistry()

    async def h() -> str:
        return "x"

    reg.register("s1", h)
    assert reg.pop("s1") is h
    # Popped once -> gone (a completed step is served from history, not re-run).
    assert reg.pop("s1") is None


def test_registry_pop_missing_is_none():
    assert StepHandlerRegistry().pop("nope") is None
