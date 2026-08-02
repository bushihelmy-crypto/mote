#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``mote.runtime.durable.factory`` — backend dispatch.

``make_durable_backend`` maps ``DurableConfig.backend`` onto a concrete
:class:`DurableBackend`. The JSONL branch is zero-dependency; the Temporal branch
imports the optional package lazily and DEGRADES to the JSONL tier (never
crashes) when the ``[temporal]`` extra is absent (``ImportError``) or the backend
is not yet built (``NotImplementedError``). A run must always end up with a real
durable backend.
"""

from __future__ import annotations

import pytest

from mote.contracts.config.tool import DurableConfig
from mote.runtime.durable import DurableBackend, JsonlBackend, make_durable_backend
from mote.runtime.ledger import RunJournal
from mote.runtime.session.workspace import SessionWorkspace


def _journal(tmp_path, session_id="sess") -> RunJournal:
    return RunJournal(session_id, store=SessionWorkspace(root=str(tmp_path)))


def test_durable_config_defaults_are_zero_dependency():
    # Default = the always-on JSONL tier; nothing optional selected.
    cfg = DurableConfig()
    assert cfg.enabled is True
    assert cfg.backend == "jsonl"
    # TemporalConfig is composed but inert under the default backend.
    assert cfg.temporal.server_address == "localhost:7233"
    assert cfg.temporal.namespace == "default"
    assert cfg.temporal.task_queue == "mote"


def test_temporal_config_has_per_seam_activity_policy():
    from mote.contracts.config.tool import ActivityConfig, TemporalConfig

    tc = TemporalConfig()
    for seam in (tc.tool_activity, tc.think_activity, tc.timer_activity):
        assert isinstance(seam, ActivityConfig)
        # sane per-seam defaults: unbounded attempts (deferring to timeouts),
        # standard backoff curve, no server timeout override.
        assert seam.max_retry_attempts == 0
        assert seam.initial_retry_interval_seconds == 1.0
        assert seam.retry_backoff_coefficient == 2.0
        assert seam.start_to_close_timeout_seconds is None
        assert seam.non_retryable_error_types == []


def test_jsonl_backend_is_the_default(tmp_path):
    journal = _journal(tmp_path)
    backend = make_durable_backend(DurableConfig(), journal)
    assert isinstance(backend, JsonlBackend)
    assert isinstance(backend, DurableBackend)
    # borrows the SAME journal (no second instance)
    assert backend.journal is journal


def test_temporal_cannot_activate_through_runtime_factory(tmp_path):
    with pytest.raises(RuntimeError, match="Product Workflow application owner"):
        make_durable_backend(DurableConfig(backend="temporal"), _journal(tmp_path))
