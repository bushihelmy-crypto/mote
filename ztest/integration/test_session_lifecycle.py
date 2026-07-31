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

from mote.runtime.agent.component_keys import ROUTER

pytestmark = pytest.mark.asyncio


def _event_types(role):
    """Collect the rollout event ``type`` values for a role's session."""
    from mote.runtime.session import SessionLog

    log = SessionLog(role.state.session_id)
    return [rec["type"] for rec in log.iter_raw()]


async def test_run_writes_rollout(make_role, tmp_path):
    target = os.path.join(str(tmp_path), "a.txt")
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Edit"],
        turns=[[("Edit", {"file_path": target, "old_string": "", "new_string": "x"})], "done"],
    )

    await role.run(with_message="make a.txt")

    from mote.runtime.session import SessionLog

    log = SessionLog(role.state.session_id)
    assert log.exists()
    types = _event_types(role)
    # First line is the session_meta; messages + a turn boundary follow.
    assert types[0] == "session_meta"
    assert "message" in types
    assert "turn_context" in types

    # Ordering guarantee: the user's own prompt is committed to the rollout
    # before the per-turn <system-reminder> turn-context block (both are `message`
    # records). The loop commits the prompt via _observe, then records the
    # persistent turn-context right before think — so the durable log reads
    # prompt -> turn-context, never the reverse.
    records = list(log.iter_raw())
    prompt_idx = next(
        i for i, r in enumerate(records) if r["type"] == "message" and r["payload"].get("content") == "make a.txt"
    )
    reminder_idx = next(
        i
        for i, r in enumerate(records)
        if r["type"] == "message" and str(r["payload"].get("content", "")).startswith("<system-reminder>")
    )
    assert prompt_idx < reminder_idx


async def test_resume_rebuilds_history(make_role, context, tmp_path):
    target = os.path.join(str(tmp_path), "b.txt")
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Edit"],
        turns=[[("Edit", {"file_path": target, "old_string": "", "new_string": "y"})], "finished"],
    )
    await role.run(with_message="make b.txt")
    sid = role.state.session_id
    original = [m.content for m in role.context_manager.get()]
    assert original  # non-empty history

    # A brand-new Role pinned to the same session_id rebuilds the history from
    # the rollout, without re-running anything.
    from mote.runtime.agent import AgentWiring, Role
    from mote.runtime.agent.role_schema import RoleSchema

    revived = Role(
        role_schema=RoleSchema(name="Tester", tools=["Edit"]),
        wiring=AgentWiring.for_context(context),
    )
    revived.state.session_id = sid

    assert revived.resume_session() is True
    rebuilt = [m.content for m in revived.state.context.messages]
    assert rebuilt == original
    # Working-dir anchor was restored from the session_meta.
    assert revived.state.working_dir == str(tmp_path)
    assert revived.state.recovered is True


async def test_resume_without_log_returns_false(context):
    from mote.runtime.agent import AgentWiring, Role
    from mote.runtime.agent.role_schema import RoleSchema

    role = Role(role_schema=RoleSchema(name="Ghost"), wiring=AgentWiring.for_context(context))
    assert role.resume_session() is False


async def test_fork_branches_independent_child(make_role, tmp_path):
    target = os.path.join(str(tmp_path), "c.txt")
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Edit"],
        turns=[[("Edit", {"file_path": target, "old_string": "", "new_string": "z"})], "finished"],
    )
    await role.run(with_message="make c.txt")
    parent_sid = role.state.session_id
    parent_history = [m.content for m in role.context_manager.get()]

    child = role.fork_session()

    # Lineage recorded; child got a fresh session id with its own rollout.
    assert child.state.parent_session_id == parent_sid
    assert child.state.session_id != parent_sid

    from mote.runtime.session import SessionLog

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
        tools=["Edit"],
        turns=[[("Edit", {"file_path": one, "old_string": "", "new_string": "1"})], "first done"],
    )
    await role.run(with_message="first task")
    sid = role.state.session_id

    # A fresh Role pinned to the same session resumes the history, then runs a
    # *new* task. Both the rebuilt past and the new turn coexist in memory.
    two = os.path.join(str(tmp_path), "two.txt")
    revived = make_role(
        working_dir=str(tmp_path),
        tools=["Edit"],
        turns=[[("Edit", {"file_path": two, "old_string": "", "new_string": "2"})], "second done"],
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
    from mote.runtime.session import SessionLog

    p_file = os.path.join(str(tmp_path), "p.txt")
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Edit"],
        turns=[[("Edit", {"file_path": p_file, "old_string": "", "new_string": "p"})], "parent done"],
    )
    await role.run(with_message="parent task")
    parent_sid = role.state.session_id
    parent_types_before = [r["type"] for r in SessionLog(parent_sid).iter_raw()]

    child = role.fork_session()

    # Re-script the child with its own turns and run it.
    from .conftest import ScriptedLLM, ScriptedRouter

    c_file = os.path.join(str(tmp_path), "c.txt")
    llm = ScriptedLLM([[("Edit", {"file_path": c_file, "old_string": "", "new_string": "c"})], "child done"])
    child._components._graph.seed(ROUTER, ScriptedRouter(llm))
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


async def test_resume_reaps_think_already_in_history(make_role, context, tmp_path):
    """The G1 double-record guard, wired through a real resume.

    A completed think whose assistant message IS already durable in the rebuilt
    history must be reaped on resume — reinstating it would double-record that
    assistant turn. We seed such a record into the run journal after a real run,
    then prove ``resume_session`` (via ``_reconcile_think_journal``) drops it.
    """
    from mote.contracts.model.inference import InferenceResult
    from mote.runtime.agent import AgentWiring, Role
    from mote.runtime.agent.role_schema import RoleSchema
    from mote.runtime.ledger import KIND_THINK

    role = make_role(
        working_dir=str(tmp_path),
        tools=["Edit"],
        turns=[
            [("Edit", {"file_path": os.path.join(str(tmp_path), "g.txt"), "old_string": "", "new_string": "g"})],
            "all done",
        ],
    )
    await role.run(with_message="go")
    sid = role.state.session_id

    # Pick the last assistant message the run committed to history; seed a
    # completed think whose payload matches it exactly (so it IS present).
    last_ai = next(m for m in reversed(role.context_manager.get()) if m.is_ai_message())
    journal = role.executor.journal
    assert journal is not None
    journal.record_started("think:99", KIND_THINK, "pure", seq=99)
    journal.record_completed("think:99", payload=InferenceResult(content=last_ai.content or "").model_dump_json())
    assert journal.replay("think:99") is not None

    revived = Role(
        role_schema=RoleSchema(name="Tester", tools=["Edit"]),
        wiring=AgentWiring.for_context(context),
    )
    revived.state.session_id = sid
    assert revived.resume_session() is True

    # The matched completed think was reaped by the resume guard.
    assert revived.executor.journal is not None
    assert revived.executor.journal.replay("think:99") is None


async def test_resume_keeps_unmatched_completed_think(make_role, context, tmp_path):
    """A completed think whose assistant message never reached history survives.

    This is the reinstate candidate: a crash between the model returning and the
    turn being recorded. The resume guard must LEAVE it so the loop can reinstate
    it (skip the LLM) on its first think.
    """
    from mote.contracts.model.inference import InferenceResult
    from mote.runtime.agent import AgentWiring, Role
    from mote.runtime.agent.role_schema import RoleSchema
    from mote.runtime.ledger import KIND_THINK

    role = make_role(
        working_dir=str(tmp_path),
        tools=["Edit"],
        turns=[
            [("Edit", {"file_path": os.path.join(str(tmp_path), "h.txt"), "old_string": "", "new_string": "h"})],
            "all done",
        ],
    )
    await role.run(with_message="go")
    sid = role.state.session_id

    journal = role.executor.journal
    assert journal is not None
    journal.record_started("think:99", KIND_THINK, "pure", seq=99)
    journal.record_completed(
        "think:99", payload=InferenceResult(content="a thought that never reached history").model_dump_json()
    )

    revived = Role(
        role_schema=RoleSchema(name="Tester", tools=["Edit"]),
        wiring=AgentWiring.for_context(context),
    )
    revived.state.session_id = sid
    assert revived.resume_session() is True

    assert revived.executor.journal is not None
    rec = revived.executor.journal.replay("think:99")
    assert rec is not None and rec.status == "completed"


async def test_resume_continues_durable_timer_by_remaining_time(make_role, context, tmp_path):
    """The G4 durable-timer resume, wired through a real Role's capability.

    A bounded Sleep journals a wall-clock deadline. If a crash strikes mid-wait,
    a fresh Role's first ``wait_interruptible`` must continue for the time
    REMAINING to that deadline, not restart the full countdown. We seed an
    in-flight timer whose deadline is ~100s out, then prove the resumed role's
    capability adopts it (waits ~100s remaining, not a fresh 5s) — asserted via
    the resolved remaining time, without actually sleeping.
    """
    import time as _time

    from mote.runtime.agent import AgentWiring, Role
    from mote.runtime.agent.role_schema import RoleSchema
    from mote.runtime.ledger import KIND_TIMER

    role = make_role(
        working_dir=str(tmp_path),
        tools=["Edit"],
        turns=[
            [("Edit", {"file_path": os.path.join(str(tmp_path), "t.txt"), "old_string": "", "new_string": "t"})],
            "done",
        ],
    )
    await role.run(with_message="go")
    sid = role.state.session_id

    # Seed an in-flight durable timer whose deadline is ~100s in the future.
    journal = role.executor.journal
    assert journal is not None
    deadline = _time.time() + 100.0
    journal.record_started("timer:1", KIND_TIMER, "pure", seq=1, payload=repr(deadline))

    revived = Role(
        role_schema=RoleSchema(name="Tester", tools=["Edit"]),
        wiring=AgentWiring.for_context(context),
    )
    revived.state.session_id = sid
    assert revived.resume_session() is True

    # The resumed capability adopts the in-flight timer's remaining time.
    from mote.runtime.durable import resume_timer

    rj = revived.executor.journal
    assert rj is not None
    resumed = resume_timer(rj)
    assert resumed is not None
    step_id, resumed_deadline = resumed
    assert step_id == "timer:1"
    # Remaining time is close to 100s (the seeded deadline), NOT a fresh countdown.
    assert 90.0 < (resumed_deadline - _time.time()) <= 100.0


async def test_list_sessions_sees_run(make_role, tmp_path, redirect_sessions):
    role = make_role(
        working_dir=str(tmp_path),
        tools=["Edit"],
        turns=[
            [("Edit", {"file_path": os.path.join(str(tmp_path), "d.txt"), "old_string": "", "new_string": "d"})],
            "done",
        ],
    )
    await role.run(with_message="go")

    from mote.runtime.session import list_sessions

    infos = list_sessions(str(redirect_sessions))
    ids = [i.session_id for i in infos]
    assert role.state.session_id in ids
