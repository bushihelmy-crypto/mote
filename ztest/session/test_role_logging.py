#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests that a Role wires the session log end-to-end.

Covers: ``Role.session_recorder`` builds the rollout and writes a session_meta
first line carrying the role's session_id; the recorder is injected into the
ContextManager so added messages are persisted; ``_record_turn_boundary``
appends a turn_context event. The full ``run()`` path is exercised elsewhere;
here we drive the seams directly to stay offline.
"""
from __future__ import annotations

import pytest

from metagpt.common.schema import UserMessage
from metagpt.roles import Role


@pytest.fixture
def role_in_tmp(tmp_path, monkeypatch):
    from metagpt.router.llm.context import Context

    # Redirect all session logs to the temp dir.
    monkeypatch.setattr("metagpt.session.log._default_base_dir", lambda: tmp_path)
    return Role(name="Logger", context=Context())


def test_session_recorder_writes_meta_first_line(role_in_tmp):
    recorder = role_in_tmp.session_recorder
    log = recorder.log
    assert log.exists()
    records = list(log.iter_raw())
    assert records[0]["type"] == "session_meta"
    assert records[0]["payload"]["session_id"] == role_in_tmp.session_id
    assert records[0]["payload"]["role_class"].endswith("Role")


def test_context_manager_uses_injected_recorder(role_in_tmp):
    role_in_tmp.context_manager.add(UserMessage(content="persist me"))
    records = list(role_in_tmp.session_recorder.log.iter_raw())
    types = [r["type"] for r in records]
    assert "message" in types
    msg_rec = next(r for r in records if r["type"] == "message")
    assert msg_rec["payload"]["content"] == "persist me"


def test_record_turn_boundary_appends_turn_context(role_in_tmp):
    _ = role_in_tmp.session_recorder  # ensure recorder built
    role_in_tmp._record_turn_boundary()
    records = list(role_in_tmp.session_recorder.log.iter_raw())
    turn = [r for r in records if r["type"] == "turn_context"]
    assert len(turn) == 1
    assert "turn_id" in turn[0]["payload"]


def test_turn_boundary_noop_without_recorder(role_in_tmp):
    # Never touched session_recorder -> _session_recorder is None -> safe no-op.
    assert role_in_tmp._session_recorder is None
    role_in_tmp._record_turn_boundary()  # must not raise
    assert role_in_tmp._session_recorder is None


def test_resume_session_missing_log_returns_false(tmp_path, monkeypatch):
    from metagpt.router.llm.context import Context

    monkeypatch.setattr("metagpt.session.log._default_base_dir", lambda: tmp_path)
    role = Role(name="NoLog", context=Context())
    assert role.resume_session() is False
    assert role.state.recovered is False


def test_resume_session_rebuilds_history(tmp_path, monkeypatch):
    from metagpt.router.llm.context import Context

    monkeypatch.setattr("metagpt.session.log._default_base_dir", lambda: tmp_path)

    # Session A writes some history through the live recorder path.
    role_a = Role(name="A", context=Context())
    sid = role_a.session_id
    role_a.context_manager.add(UserMessage(content="first"))
    role_a.context_manager.add(UserMessage(content="second"))

    # Session B is a fresh role pinned to the same session_id; resume rebuilds.
    role_b = Role(name="B", context=Context())
    role_b.state.session_id = sid
    assert role_b.resume_session() is True
    assert role_b.state.recovered is True
    assert [m.content for m in role_b.context_manager.get()] == ["first", "second"]


def test_resume_does_not_re_record_replayed_history(tmp_path, monkeypatch):
    from metagpt.router.llm.context import Context

    monkeypatch.setattr("metagpt.session.log._default_base_dir", lambda: tmp_path)

    role_a = Role(name="A", context=Context())
    sid = role_a.session_id
    role_a.context_manager.add(UserMessage(content="one"))

    role_b = Role(name="B", context=Context())
    role_b.state.session_id = sid
    role_b.resume_session()
    # A new live message after resume appends exactly once; replayed history is
    # not re-recorded (assigned straight into the backing context).
    role_b.context_manager.add(UserMessage(content="two"))

    from metagpt.session.log import SessionLog

    msgs = [r for r in SessionLog(sid, base_dir=str(tmp_path)).iter_raw() if r["type"] == "message"]
    assert [m["payload"]["content"] for m in msgs] == ["one", "two"]
