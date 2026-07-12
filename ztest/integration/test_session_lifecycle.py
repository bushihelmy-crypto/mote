#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""End-to-end session persistence: run → resume → fork.

A real ``Role.run`` streams its conversation into an append-only
``rollout.jsonl`` (the durable truth source). These tests verify the full
round-trip: the log is written during a run, a fresh Role rebuilds the same
history via ``resume_session``, and ``fork_session`` branches an independent
child with recorded lineage. The session base dir is redirected to tmp by the
``redirect_sessions`` autouse fixture.
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.asyncio


def _event_types(role):
    """Collect the rollout event ``type`` values for a role's session."""
    from metagpt.session import SessionLog

    log = SessionLog(role.state.session_id)
    return [rec["type"] for rec in log.iter_raw()]


async def test_run_writes_rollout(make_role, tmp_path):
    target = os.path.join(str(tmp_path), "a.txt")
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Write"],
        turns=[[("Write", {"file_path": target, "content": "x"})], "done"],
    )

    await role.run(with_message="make a.txt")

    from metagpt.session import SessionLog

    log = SessionLog(role.state.session_id)
    assert log.exists()
    types = _event_types(role)
    # First line is the session_meta; messages + a turn boundary follow.
    assert types[0] == "session_meta"
    assert "message" in types
    assert "turn_context" in types


async def test_resume_rebuilds_history(make_role, context, tmp_path):
    target = os.path.join(str(tmp_path), "b.txt")
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Write"],
        turns=[[("Write", {"file_path": target, "content": "y"})], "finished"],
    )
    await role.run(with_message="make b.txt")
    sid = role.state.session_id
    original = [m.content for m in role.context_manager.get()]
    assert original  # non-empty history

    # A brand-new Role pinned to the same session_id rebuilds the history from
    # the rollout, without re-running anything.
    from metagpt.roles import Role
    from metagpt.roles.role_schema import RoleSchema

    revived = Role(role_schema=RoleSchema(name="Tester", tools=["Write"]), context=context)
    revived.state.session_id = sid

    assert revived.resume_session() is True
    rebuilt = [m.content for m in revived.state.context.messages]
    assert rebuilt == original
    # Working-dir anchor was restored from the session_meta.
    assert revived.state.working_dir == str(tmp_path)
    assert revived.state.recovered is True


async def test_resume_without_log_returns_false(context):
    from metagpt.roles import Role
    from metagpt.roles.role_schema import RoleSchema

    role = Role(role_schema=RoleSchema(name="Ghost"), context=context)
    assert role.resume_session() is False


async def test_fork_branches_independent_child(make_role, tmp_path):
    target = os.path.join(str(tmp_path), "c.txt")
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Write"],
        turns=[[("Write", {"file_path": target, "content": "z"})], "finished"],
    )
    await role.run(with_message="make c.txt")
    parent_sid = role.state.session_id
    parent_history = [m.content for m in role.context_manager.get()]

    child = role.fork_session()

    # Lineage recorded; child got a fresh session id with its own rollout.
    assert child.state.parent_session_id == parent_sid
    assert child.state.session_id != parent_sid

    from metagpt.session import SessionLog

    assert SessionLog(child.state.session_id).exists()

    # Child inherited the parent's final history.
    child_history = [m.content for m in child.state.context.messages]
    assert child_history == parent_history

    # The child's session_meta carries the parent lineage.
    child_meta = next(rec for rec in SessionLog(child.state.session_id).iter_raw())
    assert child_meta["type"] == "session_meta"
    assert child_meta["payload"]["parent_session_id"] == parent_sid


async def test_resume_then_continue_running(make_role, context, tmp_path):
    """A resumed Role keeps its rebuilt history and appends a fresh turn."""
    one = os.path.join(str(tmp_path), "one.txt")
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Write"],
        turns=[[("Write", {"file_path": one, "content": "1"})], "first done"],
    )
    await role.run(with_message="first task")
    sid = role.state.session_id

    # A fresh Role pinned to the same session resumes the history, then runs a
    # *new* task. Both the rebuilt past and the new turn coexist in memory.
    two = os.path.join(str(tmp_path), "two.txt")
    revived = make_role(
        working_dir=str(tmp_path),
        tools=["Write"],
        turns=[[("Write", {"file_path": two, "content": "2"})], "second done"],
    )
    revived.state.session_id = sid
    assert revived.resume_session() is True

    await revived.run(with_message="second task")

    # The new task ran against the disk.
    assert os.path.exists(two)
    contents = [m.content for m in revived.context_manager.get()]
    # Both the resumed history and the freshly-run turn are present.
    assert any("first task" in c for c in contents)
    assert any("second task" in c for c in contents)


async def test_fork_child_runs_independently(make_role, tmp_path):
    """A forked child can run new work without disturbing the parent's log."""
    from metagpt.session import SessionLog

    p_file = os.path.join(str(tmp_path), "p.txt")
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Write"],
        turns=[[("Write", {"file_path": p_file, "content": "p"})], "parent done"],
    )
    await role.run(with_message="parent task")
    parent_sid = role.state.session_id
    parent_types_before = [r["type"] for r in SessionLog(parent_sid).iter_raw()]

    child = role.fork_session()

    # Re-script the child with its own turns and run it.
    from .conftest import ScriptedLLM, ScriptedRouter

    c_file = os.path.join(str(tmp_path), "c.txt")
    llm = ScriptedLLM([[("Write", {"file_path": c_file, "content": "c"})], "child done"])
    child._components._router = ScriptedRouter(llm)
    child.scripted_llm = llm  # type: ignore[attr-defined]

    await child.run(with_message="child task")

    # The child did its own work.
    assert os.path.exists(c_file)
    # The parent's rollout is untouched by the child's run.
    parent_types_after = [r["type"] for r in SessionLog(parent_sid).iter_raw()]
    assert parent_types_before == parent_types_after
    # The child sees the inherited parent history *and* its own new turn.
    child_contents = [m.content for m in child.context_manager.get()]
    assert any("parent task" in c for c in child_contents)
    assert any("child task" in c for c in child_contents)


async def test_list_sessions_sees_run(make_role, tmp_path, redirect_sessions):
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Write"],
        turns=[[("Write", {"file_path": os.path.join(str(tmp_path), "d.txt"), "content": "d"})], "done"],
    )
    await role.run(with_message="go")

    from metagpt.session import list_sessions

    infos = list_sessions(str(redirect_sessions))
    ids = [i.session_id for i in infos]
    assert role.state.session_id in ids
