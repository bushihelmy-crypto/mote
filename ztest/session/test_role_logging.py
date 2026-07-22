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


def test_resume_stages_only_unfinished_output_state(role_in_tmp, monkeypatch):
    from types import SimpleNamespace

    unfinished = {
        "status": "awaiting_correction",
        "contract_id": "mote.text@1",
        "schema_fingerprint": role_in_tmp.output_contract.decoder.schema.fingerprint,
        "correction_attempts": 1,
    }
    monkeypatch.setattr(
        "mote.roles.session_manager.replay",
        lambda _log: SimpleNamespace(
            meta={},
            messages=[],
            terminal_state=None,
            kernel_state=None,
            browser_state=None,
            output_state=unfinished,
        ),
    )
    monkeypatch.setattr("mote.roles.session_manager.SessionLog.exists", lambda _self: True)

    assert role_in_tmp.resume_session() is True
    assert role_in_tmp._state_ctl.take_pending_output_restore() == unfinished
    assert role_in_tmp._state_ctl.take_pending_output_restore() is None


def test_resume_never_stages_graph_output_as_agent_output(role_in_tmp, monkeypatch):
    from types import SimpleNamespace

    graph_state = {
        "status": "committed",
        "run_id": "graph-1",
        "run_kind": "graph",
        "contract_id": "test.graph@1",
        "schema_fingerprint": "graph-schema",
    }
    monkeypatch.setattr(
        "mote.roles.session_manager.replay",
        lambda _log: SimpleNamespace(
            meta={},
            messages=[],
            terminal_state=None,
            kernel_state=None,
            browser_state=None,
            output_state=graph_state,
            output_states={"graph-1": graph_state},
        ),
    )
    monkeypatch.setattr("mote.roles.session_manager.SessionLog.exists", lambda _self: True)

    assert role_in_tmp.resume_session() is True
    assert role_in_tmp._state_ctl.take_pending_output_restore() is None


@pytest.mark.asyncio
async def test_committed_graph_output_resumes_by_stable_run_id(role_in_tmp):
    from mote.executor.tasks.bggraph.spec import GraphOutputContractSpec
    from mote.roles.output_contract import JsonSchemaOutputDecoder

    schema = {"type": "integer"}
    fingerprint = JsonSchemaOutputDecoder(schema).schema.fingerprint
    role_in_tmp._state_ctl.set_pending_graph_output_restores(
        {
            "tool-call-1": {
                "status": "committed",
                "candidate_id": "candidate-1",
                "contract_id": "test.integer@1",
                "schema_fingerprint": fingerprint,
                "value": 42,
                "correction_attempts": 0,
                "run_id": "tool-call-1",
                "run_kind": "graph",
            }
        }
    )

    async with role_in_tmp.graph_run_lease("tool-call-1"):
        committed = await role_in_tmp.resume_graph_output(
            contract_spec=GraphOutputContractSpec(namespace="test", name="integer", version="1", schema=schema),
            run_id="tool-call-1",
        )

    assert committed is not None
    assert committed.value == 42
    assert committed.run_id == "tool-call-1"
    async with role_in_tmp.graph_run_lease("tool-call-1"):
        assert (
            await role_in_tmp.resume_graph_output(
                contract_spec=GraphOutputContractSpec(namespace="test", name="integer", version="1", schema=schema),
                run_id="tool-call-1",
            )
            is None
        )


@pytest.mark.asyncio
async def test_concurrent_graph_resume_has_one_live_owner(role_in_tmp):
    from mote.common.exception import RunLeaseUnavailableError
    from mote.router.llm.context import Context

    contender = Role(name="Contender", context=Context())
    contender.state.session_id = role_in_tmp.session_id

    async with role_in_tmp.graph_run_lease("graph-run-1"):
        with pytest.raises(RunLeaseUnavailableError):
            async with contender.graph_run_lease("graph-run-1"):
                pass


@pytest.mark.asyncio
async def test_role_accepts_replaceable_lease_coordinator(tmp_path):
    from mote.common.exception import OutputCommitFencedError
    from mote.router.llm.context import Context
    from mote.session.run_lease import RunLeaseStore

    coordinator = RunLeaseStore(tmp_path / "external-coordinator.json")
    role = Role(
        name="ExternalCoordinator",
        context=Context(),
        run_lease_coordinator=coordinator,
    )

    async with role.graph_run_lease("graph-run-1"):
        current = coordinator.get("graph-run-1")
        assert current is not None
        assert current.owner_id

    with pytest.raises(OutputCommitFencedError) as caught:
        coordinator.assert_current("graph-run-1", current.fencing_token)
    assert caught.value.code.value == "OUTPUT_COMMIT_FENCED"


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
