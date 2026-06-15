#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests that a Role wires the session log end-to-end.

Covers: ``Role.session_log`` builds the rollout and writes a session_meta first
line carrying the role's session_id; the event bus's ``RecorderSubscriber`` is
wired so messages added through the ContextManager are persisted;
``_emit_turn_end`` appends a turn_context event. The full ``run()`` path is
exercised elsewhere; here we drive the seams directly to stay offline.
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


def test_session_log_writes_meta_first_line(role_in_tmp):
    log = role_in_tmp.session_log
    assert log.exists()
    records = list(log.iter_raw())
    assert records[0]["type"] == "session_meta"
    assert records[0]["payload"]["session_id"] == role_in_tmp.session_id
    assert records[0]["payload"]["role_class"].endswith("Role")


@pytest.mark.asyncio
async def test_context_manager_messages_are_recorded(role_in_tmp):
    await role_in_tmp.context_manager.add(UserMessage(content="persist me"))
    records = list(role_in_tmp.session_log.iter_raw())
    types = [r["type"] for r in records]
    assert "message" in types
    msg_rec = next(r for r in records if r["type"] == "message")
    assert msg_rec["payload"]["content"] == "persist me"


@pytest.mark.asyncio
async def test_emit_turn_end_appends_turn_context(role_in_tmp):
    _ = role_in_tmp.event_bus  # ensure bus + recorder built
    await role_in_tmp._emit_turn_end()
    records = list(role_in_tmp.session_log.iter_raw())
    turn = [r for r in records if r["type"] == "turn_context"]
    assert len(turn) == 1
    assert "turn_id" in turn[0]["payload"]


@pytest.mark.asyncio
async def test_emit_turn_end_noop_without_bus(role_in_tmp):
    # Never touched event_bus -> _event_bus is None -> safe no-op.
    assert role_in_tmp._event_bus is None
    await role_in_tmp._emit_turn_end()  # must not raise
    assert role_in_tmp._event_bus is None


def test_resume_session_missing_log_returns_false(tmp_path, monkeypatch):
    from metagpt.router.llm.context import Context

    monkeypatch.setattr("metagpt.session.log._default_base_dir", lambda: tmp_path)
    role = Role(name="NoLog", context=Context())
    assert role.resume_session() is False
    assert role.state.recovered is False


@pytest.mark.asyncio
async def test_resume_session_rebuilds_history(tmp_path, monkeypatch):
    from metagpt.router.llm.context import Context

    monkeypatch.setattr("metagpt.session.log._default_base_dir", lambda: tmp_path)

    # Session A writes some history through the live recorder path.
    role_a = Role(name="A", context=Context())
    sid = role_a.session_id
    await role_a.context_manager.add(UserMessage(content="first"))
    await role_a.context_manager.add(UserMessage(content="second"))

    # Session B is a fresh role pinned to the same session_id; resume rebuilds.
    role_b = Role(name="B", context=Context())
    role_b.state.session_id = sid
    assert role_b.resume_session() is True
    assert role_b.state.recovered is True
    assert [m.content for m in role_b.context_manager.get()] == ["first", "second"]


@pytest.mark.asyncio
async def test_resume_does_not_re_record_replayed_history(tmp_path, monkeypatch):
    from metagpt.router.llm.context import Context

    monkeypatch.setattr("metagpt.session.log._default_base_dir", lambda: tmp_path)

    role_a = Role(name="A", context=Context())
    sid = role_a.session_id
    await role_a.context_manager.add(UserMessage(content="one"))

    role_b = Role(name="B", context=Context())
    role_b.state.session_id = sid
    role_b.resume_session()
    # A new live message after resume appends exactly once; replayed history is
    # not re-recorded (assigned straight into the backing context).
    await role_b.context_manager.add(UserMessage(content="two"))

    from metagpt.session.log import SessionLog

    msgs = [r for r in SessionLog(sid, base_dir=str(tmp_path)).iter_raw() if r["type"] == "message"]
    assert [m["payload"]["content"] for m in msgs] == ["one", "two"]
