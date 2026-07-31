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


def test_temporal_selected_without_extra_degrades_to_jsonl(tmp_path, monkeypatch):
    # A missing ``[temporal]`` extra makes the lazy import raise ImportError → the
    # factory degrades to the always-on JSONL tier. Forced deterministically here
    # (raise ImportError from the seam) so the test holds WHETHER OR NOT temporalio
    # is installed in the dev env — it exercises the degrade branch, not the ambient
    # dependency state.
    def _no_extra(config, journal):
        raise ImportError("No module named 'temporalio'")

    _install_fake_temporal(monkeypatch, _no_extra)
    journal = _journal(tmp_path)
    backend = make_durable_backend(DurableConfig(backend="temporal"), journal)
    assert isinstance(backend, JsonlBackend)
    assert backend.journal is journal


def _install_fake_temporal(monkeypatch, make_fn):
    """Inject a fake ``mote.runtime.durable.temporal`` so the factory's lazy import
    resolves it WITHOUT requiring the (absent) ``temporalio`` extra.

    Lets us exercise the factory's use/degrade branches independent of whether
    the optional dependency is installed in the test env.
    """
    import sys
    import types

    fake = types.ModuleType("mote.runtime.durable.temporal")
    fake.make_temporal_backend = make_fn  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mote.runtime.durable.temporal", fake)


def test_temporal_not_yet_built_degrades_to_jsonl(tmp_path, monkeypatch):
    # Simulate the extra being INSTALLED but the backend not yet implemented:
    # make_temporal_backend raises NotImplementedError. The factory must still
    # degrade to JSONL rather than propagate.
    def _not_built(config, journal):
        raise NotImplementedError("backend under construction")

    _install_fake_temporal(monkeypatch, _not_built)
    journal = _journal(tmp_path)
    backend = make_durable_backend(DurableConfig(backend="temporal"), journal)
    assert isinstance(backend, JsonlBackend)


def test_temporal_backend_used_when_available(tmp_path, monkeypatch):
    # When make_temporal_backend returns a real backend, the factory uses it
    # verbatim (no degrade). Prove the happy path wires through.
    sentinel = JsonlBackend(_journal(tmp_path, "temporal-sentinel"))

    def _ok(config, journal):
        return sentinel

    _install_fake_temporal(monkeypatch, _ok)
    backend = make_durable_backend(DurableConfig(backend="temporal"), _journal(tmp_path))
    assert backend is sentinel


def test_degrade_never_raises_and_stays_durable(tmp_path, monkeypatch):
    # The whole point of degrading: selecting an unavailable backend must never
    # take a run down — it always returns a live durable backend. Forced via a
    # raising seam so the invariant holds regardless of ambient temporalio.
    def _no_extra(config, journal):
        raise ImportError("No module named 'temporalio'")

    _install_fake_temporal(monkeypatch, _no_extra)
    journal = _journal(tmp_path)
    backend = make_durable_backend(DurableConfig(backend="temporal"), journal)
    assert isinstance(backend, DurableBackend)


def test_real_temporal_backend_returned_when_extra_present(tmp_path):
    # When temporalio IS installed, the temporal branch returns the REAL
    # TemporalBackend (a live DurableBackend over the shared journal) — no degrade.
    # Skips cleanly when the extra is absent so the core env stays green.
    import pytest

    pytest.importorskip("temporalio")
    from mote.runtime.durable.temporal import TemporalBackend

    journal = _journal(tmp_path)
    backend = make_durable_backend(DurableConfig(backend="temporal"), journal)
    assert isinstance(backend, TemporalBackend)
    assert isinstance(backend, DurableBackend)
    assert backend.journal is journal
