#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for ``metagpt.session.terminal_state`` — the terminal-state recorder.

Covers: a record appends a ``terminal_state`` event to the shared rollout log
(cwd + env diff + unset); the ``enabled`` gate suppresses recording; recording
is best-effort and conforms to the ``TerminalStateStore`` protocol.
"""
from __future__ import annotations

from metagpt.common.interface import TerminalStateStore
from metagpt.session.events import TERMINAL_STATE
from metagpt.session.log import SessionLog
from metagpt.session.terminal_state import TerminalStateRecorder


def _recorder(tmp_path, **kw):
    log = SessionLog("term_sess", base_dir=str(tmp_path))
    return TerminalStateRecorder(log, **kw), log


def test_record_appends_terminal_state_event(tmp_path):
    rec, log = _recorder(tmp_path)
    rec.record("/tmp/work", {"FOO": "bar"}, ["OLD"], tool="Terminal")

    records = list(log.iter_raw())
    assert len(records) == 1
    assert records[0]["type"] == TERMINAL_STATE
    payload = records[0]["payload"]
    assert payload["cwd"] == "/tmp/work"
    assert payload["env"] == {"FOO": "bar"}
    assert payload["unset"] == ["OLD"]
    assert payload["tool"] == "Terminal"


def test_disabled_gate_suppresses_recording(tmp_path):
    rec, log = _recorder(tmp_path, enabled=False)
    rec.record("/tmp", {"FOO": "bar"}, [])
    assert list(log.iter_raw()) == []


def test_record_copies_inputs(tmp_path):
    """Mutating the caller's env/unset after record must not affect the event."""
    rec, log = _recorder(tmp_path)
    env = {"FOO": "bar"}
    unset = ["OLD"]
    rec.record("/tmp", env, unset)
    env["FOO"] = "mutated"
    unset.append("MORE")

    payload = list(log.iter_raw())[0]["payload"]
    assert payload["env"] == {"FOO": "bar"}
    assert payload["unset"] == ["OLD"]


def test_recorder_conforms_to_protocol(tmp_path):
    rec, _ = _recorder(tmp_path)
    assert isinstance(rec, TerminalStateStore)


def test_record_is_best_effort_on_log_failure(tmp_path):
    rec, log = _recorder(tmp_path)

    def boom(_event):
        raise OSError("disk full")

    log.append = boom  # type: ignore[assignment]
    # Must swallow the error (never raise into the tool).
    rec.record("/tmp", {"FOO": "bar"}, [])
