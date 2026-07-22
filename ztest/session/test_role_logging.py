#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests that a Role wires the session log end-to-end.

Covers: the event bus's ``RecorderSubscriber`` is wired so messages added
through the ContextManager are persisted;
``_emit_turn_end`` appends a turn_context event. The full ``run()`` path is
exercised elsewhere; here we drive the seams directly to stay offline.
"""
from __future__ import annotations

import pytest

from mote.common.schema import ResourceMessage, UserMessage
from mote.roles import Role
from mote.session.events import SessionMetaEvent


@pytest.fixture
def role_in_tmp(tmp_path, monkeypatch):
    from mote.router.llm.context import Context

    # Redirect all session logs to the temp dir.
    monkeypatch.setattr("mote.session.log._default_base_dir", lambda: tmp_path)
    monkeypatch.setattr("mote.roles.role.bind_session_logfile", lambda _session_id: None)
    return Role(name="Logger", context=Context())


@pytest.mark.asyncio
async def test_context_manager_messages_are_recorded(role_in_tmp):
    role_in_tmp._components._wire_spine()  # wire the recorder subscriber
    await role_in_tmp.context_manager.add(UserMessage(content="persist me"))
    # iter_raw() drains queued log writes before reading them back.
    records = list(role_in_tmp.session_log.iter_raw())
    types = [r["type"] for r in records]
    assert "message" in types
    msg_rec = next(r for r in records if r["type"] == "message")
    assert msg_rec["payload"]["content"] == "persist me"


@pytest.mark.asyncio
async def test_emit_turn_end_appends_turn_context(role_in_tmp):
    role_in_tmp._components._wire_spine()  # wire the recorder (a run-lifecycle step)
    await role_in_tmp._emit_turn_end()
    records = list(role_in_tmp.session_log.iter_raw())
    turn = [r for r in records if r["type"] == "turn_context"]
    assert len(turn) == 1
    assert "turn_id" in turn[0]["payload"]


@pytest.mark.asyncio
async def test_emit_turn_end_noop_without_bus(role_in_tmp):
    # Never touched event_bus -> the slot is None -> safe no-op.
    assert role_in_tmp._components._graph.peek("event_bus") is None
    await role_in_tmp._emit_turn_end()  # must not raise
    assert role_in_tmp._components._graph.peek("event_bus") is None


def test_resume_session_missing_log_returns_false(tmp_path, monkeypatch):
    from mote.router.llm.context import Context

    monkeypatch.setattr("mote.session.log._default_base_dir", lambda: tmp_path)
    role = Role(name="NoLog", context=Context())
    assert role.resume_session() is False
    assert role.state.recovered is False


@pytest.mark.asyncio
async def test_resume_session_rebuilds_history(tmp_path, monkeypatch):
    from mote.router.llm.context import Context

    monkeypatch.setattr("mote.session.log._default_base_dir", lambda: tmp_path)

    # Session A writes some history through the live recorder path.
    role_a = Role(name="A", context=Context())
    role_a._components._wire_spine()  # wire the recorder subscriber
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
async def test_resume_refuses_mismatched_role_class(tmp_path, monkeypatch):
    """Resuming a session into a different Role class is refused fail-closed."""
    from mote.common.exception import SessionResumeIdentityError
    from mote.router.llm.context import Context

    monkeypatch.setattr("mote.session.log._default_base_dir", lambda: tmp_path)
    monkeypatch.setattr("mote.roles.role.bind_session_logfile", lambda _session_id: None)

    # Session A is created (and thus records role_class) by the base Role.
    role_a = Role(name="A", context=Context())
    role_a._components._wire_spine()
    role_a.session_log.create(
        SessionMetaEvent(
            session_id=role_a.session_id,
            role_class=f"{type(role_a).__module__}.{type(role_a).__qualname__}",
        )
    )
    sid = role_a.session_id
    await role_a.context_manager.add(UserMessage(content="first"))

    class OtherRole(Role):
        pass

    role_b = OtherRole(name="B", context=Context())
    role_b.state.session_id = sid
    with pytest.raises(SessionResumeIdentityError):
        role_b.resume_session()


def test_resume_allows_absent_recorded_role_class(tmp_path, monkeypatch):
    """A log with no recorded role_class carries no identity to check → allowed."""
    from mote.router.llm.context import Context

    monkeypatch.setattr("mote.session.log._default_base_dir", lambda: tmp_path)
    role = Role(name="Any", context=Context())
    mgr = role._session_manager
    # Absent / empty recorded identity never raises (backward compatible).
    mgr._validate_identity({})
    mgr._validate_identity({"role_class": None})
    # A matching identity also passes.
    mgr._validate_identity({"role_class": mgr._role_identity(role)})


@pytest.mark.asyncio
async def test_resume_does_not_re_record_replayed_history(tmp_path, monkeypatch):
    from mote.router.llm.context import Context

    monkeypatch.setattr("mote.session.log._default_base_dir", lambda: tmp_path)

    role_a = Role(name="A", context=Context())
    role_a._components._wire_spine()  # wire the recorder subscriber
    sid = role_a.session_id
    await role_a.context_manager.add(UserMessage(content="one"))

    role_b = Role(name="B", context=Context())
    role_b.state.session_id = sid  # pin to the same session before wiring the log
    role_b._components._wire_spine()  # wire the recorder subscriber (now bound to sid)
    role_b.resume_session()
    # A new live message after resume appends exactly once; replayed history is
    # not re-recorded (assigned straight into the backing context).
    await role_b.context_manager.add(UserMessage(content="two"))

    from mote.session.log import SessionLog

    # iter_raw() drains queued log writes before reading them back.
    msgs = [r for r in SessionLog(sid, base_dir=str(tmp_path)).iter_raw() if r["type"] == "message"]
    assert [m["payload"]["content"] for m in msgs] == ["one", "two"]


@pytest.mark.asyncio
async def test_resume_rebuilds_resource_registry(tmp_path, monkeypatch):
    from mote.router.llm.context import Context

    monkeypatch.setattr("mote.session.log._default_base_dir", lambda: tmp_path)

    # Session A records a sticky resource message (carries its id/kind/body in
    # metadata — the subclass identity is lost on dump/load, the metadata isn't).
    role_a = Role(name="A", context=Context())
    role_a._components._wire_spine()  # wire the recorder subscriber
    sid = role_a.session_id
    await role_a.context_manager.add(ResourceMessage("SKILL BODY HERE", resource_id="deploy", resource_kind="skill"))

    # Resume as a fresh role -> registry re-seeded from the replayed metadata.
    role_b = Role(name="B", context=Context())
    role_b.state.session_id = sid
    assert role_b.resume_session() is True
    registry = role_b.resource_registry
    assert "deploy" in registry
    projected = registry.project(model="gpt-4")
    assert len(projected) == 1
    assert "SKILL BODY HERE" in projected[0].content


@pytest.mark.asyncio
async def test_resume_rebuilds_task_result_pointer_with_kind(tmp_path, monkeypatch):
    from mote.common.const import RESOURCE_KIND
    from mote.router.llm.context import Context

    monkeypatch.setattr("mote.session.log._default_base_dir", lambda: tmp_path)

    # A push-once bg-task pointer rides the SAME sticky-resource seam: it is
    # recorded as a task_result ResourceMessage, so resume must rebuild it under
    # kind="task_result" (not the "skill" default) so per-kind budgeting / round
    # reaping continue to apply after a restart.
    role_a = Role(name="A", context=Context())
    role_a._components._wire_spine()
    sid = role_a.session_id
    await role_a.context_manager.add(
        ResourceMessage(
            "<task-result><task-id>bg_3</task-id></task-result>",
            resource_id="bg_3",
            resource_kind="task_result",
        )
    )

    role_b = Role(name="B", context=Context())
    role_b.state.session_id = sid
    assert role_b.resume_session() is True
    registry = role_b.resource_registry
    assert "bg_3" in registry
    (m,) = registry.project(model="gpt-4")
    assert m.resource_kind == "task_result"
    assert m.metadata[RESOURCE_KIND] == "task_result"
    assert "<task-result>" in m.content


@pytest.mark.asyncio
async def test_resume_skips_non_resource_messages(tmp_path, monkeypatch):
    from mote.router.llm.context import Context

    monkeypatch.setattr("mote.session.log._default_base_dir", lambda: tmp_path)

    role_a = Role(name="A", context=Context())
    role_a._components._wire_spine()  # wire the recorder subscriber
    sid = role_a.session_id
    await role_a.context_manager.add(UserMessage(content="plain history, no resource"))

    role_b = Role(name="B", context=Context())
    role_b.state.session_id = sid
    role_b.resume_session()
    # No resource markers in history -> registry stays empty.
    assert len(role_b.resource_registry) == 0
