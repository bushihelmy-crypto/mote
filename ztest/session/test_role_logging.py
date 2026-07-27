#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests the Role's explicit session-fact commit boundary end to end."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from mote.contracts.runtimes import (
    CheckpointFidelity,
    DriverCheckpoint,
    DriverStartResult,
    RuntimeCapabilities,
    RuntimeCheckpoint,
)
from mote.contracts.schema import ResourceMessage, UserMessage
from mote.kernel.output import text_output_contract
from mote.runtime.agent import AgentDependencies, AgentWiring, Role
from mote.runtime.events.fabric import EventFabricUnavailable
from mote.runtime.events.telemetry import TelemetryState
from mote.runtime.interactive.checkpoint_codec import decode_inline_json
from mote.runtime.models.clients.context import Context
from mote.runtime.services import EngineServices
from mote.runtime.session.codec import decode_session_event
from mote.runtime.session.events import MessageEvent, SessionMetaEvent, TurnContextEvent


class _OfflineLLM:
    """Minimal provider result used by Role's lazy compression dependency."""

    def __init__(self, model: str):
        self.model = model
        self.cost_manager = None
        self.rate_limit_tracker = None
        self.context_reducer = None

    async def aask(self, _msg, system_msgs=None, stream=True, **_kwargs):
        return "offline-summary"


class _CheckpointCaptureDriver:
    capabilities = RuntimeCapabilities(checkpoint_fidelity=CheckpointFidelity.LOGICAL)

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.started_with = None

    async def start(self, checkpoint=None):
        self.started_with = checkpoint
        return DriverStartResult(restored=checkpoint is not None)

    async def health(self):
        raise NotImplementedError

    async def checkpoint(self, reason: str):
        return DriverCheckpoint(codec="unused", schema_version=1, payload_ref="memory:unused")

    async def aclose(self):
        return None


def _offline_context() -> Context:
    from mote.ztest.model_fakes import offline_config

    return Context(
        config=offline_config(),
        provider_factory=lambda config: _OfflineLLM(config.model),
    )


def _offline_wiring(*, run_lease_coordinator=None, toolsets=()) -> AgentWiring:
    return AgentWiring(
        dependencies=AgentDependencies(
            deps=None,
            output_contract=text_output_contract(),
            toolsets=toolsets,
        ),
        services=EngineServices(
            context=_offline_context(),
            run_lease_coordinator=run_lease_coordinator,
        ),
    )


async def _initialize_session_log(role: Role) -> None:
    await role._components.start_event_fabric()
    await role.session_log.append(
        SessionMetaEvent(
            session_id=role.session_id,
            role_class=f"{type(role).__module__}.{type(role).__qualname__}",
        )
    )


async def _add_message(role: Role, message) -> None:
    await role.context_manager.add(message)


@pytest.fixture
def role_in_tmp(tmp_path, monkeypatch):
    # Redirect all session logs to the temp dir.
    monkeypatch.setattr("mote.runtime.session.log._default_base_dir", lambda: tmp_path)
    monkeypatch.setattr("mote.runtime.agent.role.bind_session_logfile", lambda *_args: None)
    return Role(name="Logger", wiring=_offline_wiring())


@pytest.mark.asyncio
async def test_context_manager_messages_are_recorded(role_in_tmp):
    await _initialize_session_log(role_in_tmp)
    await _add_message(role_in_tmp, UserMessage(content="persist me"))
    events = [decode_session_event(envelope) for envelope in role_in_tmp.session_log.iter_events()]
    message = next(event for event in events if isinstance(event, MessageEvent))
    assert message.message.content == "persist me"


@pytest.mark.asyncio
async def test_emit_turn_end_appends_turn_context(role_in_tmp):
    await _initialize_session_log(role_in_tmp)
    await role_in_tmp._emit_turn_end()
    events = [decode_session_event(envelope) for envelope in role_in_tmp.session_log.iter_events()]
    turns = [event for event in events if isinstance(event, TurnContextEvent)]
    assert len(turns) == 1
    assert turns[0].turn_id


@pytest.mark.asyncio
async def test_emit_turn_end_requires_running_event_fabric(role_in_tmp):
    assert role_in_tmp._components._graph.peek("telemetry") is None
    with pytest.raises(EventFabricUnavailable, match="not running"):
        await role_in_tmp._emit_turn_end()
    assert role_in_tmp.telemetry.state is TelemetryState.NEW


def test_resume_session_missing_log_returns_false(tmp_path, monkeypatch):
    from mote.runtime.models.clients.context import Context

    monkeypatch.setattr("mote.runtime.session.log._default_base_dir", lambda: tmp_path)
    role = Role(name="NoLog", wiring=_offline_wiring())
    assert role.resume_session() is False
    assert role.state.recovered is False


def test_resume_stages_only_unfinished_output_state(role_in_tmp, monkeypatch):
    unfinished = {
        "status": "awaiting_correction",
        "contract_id": "mote.text@1",
        "schema_fingerprint": role_in_tmp.output_contract.decoder.schema.fingerprint,
        "correction_attempts": 1,
    }
    monkeypatch.setattr(
        "mote.runtime.agent.session_manager.replay",
        lambda _log: SimpleNamespace(
            meta={},
            model_context_messages=[],
            terminal_state=None,
            kernel_state=None,
            browser_state=None,
            output_state=unfinished,
        ),
    )
    monkeypatch.setattr("mote.runtime.agent.session_manager.SessionLog.exists", lambda _self: True)

    assert role_in_tmp.resume_session() is True
    assert role_in_tmp._state_ctl.take_pending_output_restore() == unfinished
    assert role_in_tmp._state_ctl.take_pending_output_restore() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "field", "codec", "payload"),
    [
        (
            "terminal",
            "terminal_state",
            "terminal-state+json@1",
            {"cwd": "/shell", "env": {}, "unset": []},
        ),
        (
            "jupyter",
            "kernel_state",
            "jupyter-state+json@1",
            {"cwd": "/kernel", "env": {}, "unset": []},
        ),
        (
            "browser",
            "browser_state",
            "browser-state+json@1",
            {"urls": ["about:blank"], "active": 0},
        ),
    ],
)
async def test_resume_stages_domain_state_as_runtime_checkpoint(
    role_in_tmp,
    monkeypatch,
    kind,
    field,
    codec,
    payload,
):
    replayed = {
        "meta": {},
        "model_context_messages": [],
        "terminal_state": None,
        "kernel_state": None,
        "browser_state": None,
        "output_state": None,
        "output_states": {},
    }
    replayed[field] = payload
    monkeypatch.setattr(
        "mote.runtime.agent.session_manager.replay",
        lambda _log: SimpleNamespace(**replayed),
    )
    monkeypatch.setattr("mote.runtime.agent.session_manager.SessionLog.exists", lambda _self: True)

    assert role_in_tmp.resume_session() is True
    driver = _CheckpointCaptureDriver(kind)
    await role_in_tmp.runtime_host.ensure(driver)

    assert driver.started_with is not None
    assert decode_inline_json(driver.started_with, codec=codec) == payload


@pytest.mark.asyncio
async def test_resume_prefers_managed_runtime_checkpoint(role_in_tmp, monkeypatch):
    checkpoint = RuntimeCheckpoint(
        runtime_id="terminal-runtime",
        kind="terminal",
        epoch=3,
        revision=9,
        codec="terminal-state+json@1",
        schema_version=1,
        payload_ref="memory:managed",
        fidelity=CheckpointFidelity.LOGICAL,
    )
    monkeypatch.setattr(
        "mote.runtime.agent.session_manager.replay",
        lambda _log: SimpleNamespace(
            meta={},
            model_context_messages=[],
            runtime_checkpoints={"terminal": checkpoint},
            terminal_state={"cwd": "/legacy", "env": {}, "unset": []},
            kernel_state=None,
            browser_state=None,
            output_state=None,
            output_states={},
        ),
    )
    monkeypatch.setattr("mote.runtime.agent.session_manager.SessionLog.exists", lambda _self: True)

    assert role_in_tmp.resume_session() is True
    driver = _CheckpointCaptureDriver("terminal")
    await role_in_tmp.runtime_host.ensure(driver)

    assert driver.started_with == checkpoint


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
        "mote.runtime.agent.session_manager.replay",
        lambda _log: SimpleNamespace(
            meta={},
            model_context_messages=[],
            terminal_state=None,
            kernel_state=None,
            browser_state=None,
            output_state=graph_state,
            output_states={"graph-1": graph_state},
        ),
    )
    monkeypatch.setattr("mote.runtime.agent.session_manager.SessionLog.exists", lambda _self: True)

    assert role_in_tmp.resume_session() is True
    assert role_in_tmp._state_ctl.take_pending_output_restore() is None


@pytest.mark.asyncio
async def test_committed_graph_output_resumes_by_stable_run_id(role_in_tmp):
    from mote.kernel.output import JsonSchemaOutputDecoder
    from mote.orchestration.tasks.bggraph.spec import GraphOutputContractSpec

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
        committed = await role_in_tmp._components.graph_output_service.resume(
            contract_spec=GraphOutputContractSpec(namespace="test", name="integer", version="1", schema=schema),
            run_id="tool-call-1",
        )

    assert committed is not None
    assert committed.value == 42
    assert committed.run_id == "tool-call-1"
    async with role_in_tmp.graph_run_lease("tool-call-1"):
        assert (
            await role_in_tmp._components.graph_output_service.resume(
                contract_spec=GraphOutputContractSpec(namespace="test", name="integer", version="1", schema=schema),
                run_id="tool-call-1",
            )
            is None
        )


@pytest.mark.asyncio
async def test_concurrent_graph_resume_has_one_live_owner(role_in_tmp):
    from mote.runtime.errors import RunLeaseUnavailableError
    from mote.runtime.models.clients.context import Context

    contender = Role(name="Contender", wiring=_offline_wiring())
    contender.state.session_id = role_in_tmp.session_id

    async with role_in_tmp.graph_run_lease("graph-run-1"):
        with pytest.raises(RunLeaseUnavailableError):
            async with contender.graph_run_lease("graph-run-1"):
                pass


@pytest.mark.asyncio
async def test_role_accepts_replaceable_lease_coordinator(tmp_path):
    from mote.runtime.errors import OutputCommitFencedError
    from mote.runtime.models.clients.context import Context
    from mote.runtime.session.run_lease import RunLeaseStore

    coordinator = RunLeaseStore(tmp_path / "external-coordinator.json")
    role = Role(
        name="ExternalCoordinator",
        wiring=_offline_wiring(run_lease_coordinator=coordinator),
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
    from mote.runtime.models.clients.context import Context

    monkeypatch.setattr("mote.runtime.session.log._default_base_dir", lambda: tmp_path)

    # Session A writes history through the explicit durable commit boundary.
    role_a = Role(name="A", wiring=_offline_wiring())
    await _initialize_session_log(role_a)
    sid = role_a.session_id
    await _add_message(role_a, UserMessage(content="first"))
    await _add_message(role_a, UserMessage(content="second"))

    # Session B is a fresh role pinned to the same session_id; resume rebuilds.
    role_b = Role(name="B", wiring=_offline_wiring())
    role_b.state.session_id = sid
    assert role_b.resume_session() is True
    assert role_b.state.recovered is True
    assert [m.content for m in role_b.context_manager.get()] == ["first", "second"]


@pytest.mark.asyncio
async def test_resume_refuses_mismatched_role_class(tmp_path, monkeypatch):
    """Resuming a session into a different Role class is refused fail-closed."""
    from mote.runtime.errors import SessionResumeIdentityError
    from mote.runtime.models.clients.context import Context

    monkeypatch.setattr("mote.runtime.session.log._default_base_dir", lambda: tmp_path)
    monkeypatch.setattr("mote.runtime.agent.role.bind_session_logfile", lambda *_args: None)

    # Session A is created (and thus records role_class) by the base Role.
    role_a = Role(name="A", wiring=_offline_wiring())
    await _initialize_session_log(role_a)
    sid = role_a.session_id
    await _add_message(role_a, UserMessage(content="first"))

    class OtherRole(Role):
        pass

    role_b = OtherRole(name="B", wiring=_offline_wiring())
    role_b.state.session_id = sid
    with pytest.raises(SessionResumeIdentityError):
        role_b.resume_session()


def test_resume_allows_absent_recorded_role_class(tmp_path, monkeypatch):
    """A log with no recorded role_class carries no identity to check → allowed."""
    from mote.runtime.models.clients.context import Context

    monkeypatch.setattr("mote.runtime.session.log._default_base_dir", lambda: tmp_path)
    role = Role(name="Any", wiring=_offline_wiring())
    mgr = role._session_manager
    # Absent / empty recorded identity never raises (backward compatible).
    mgr.validate_identity({})
    mgr.validate_identity({"role_class": None})
    # A matching identity also passes.
    mgr.validate_identity({"role_class": mgr._role_identity(role)})


def test_resume_refuses_mismatched_toolset_manifest() -> None:
    from mote.kernel.tools.toolset import NativeToolset
    from mote.runtime.errors import SessionResumeIdentityError

    recorded = NativeToolset("workspace", (), version="1")
    current = NativeToolset("workspace", (), version="2")
    role = Role(name="Any", wiring=_offline_wiring(toolsets=(current,)))

    with pytest.raises(SessionResumeIdentityError, match="different Toolset dependencies"):
        role._session_manager.validate_identity({"toolset_manifest": [recorded.identity.to_payload()]})


def test_resume_accepts_matching_and_legacy_toolset_manifests() -> None:
    from mote.kernel.tools.toolset import NativeToolset

    tools = NativeToolset("workspace", (), version="2")
    role = Role(name="Any", wiring=_offline_wiring(toolsets=(tools,)))

    role._session_manager.validate_identity({})
    role._session_manager.validate_identity({"toolset_manifest": [tools.identity.to_payload()]})


@pytest.mark.parametrize("current_version, succeeds", [("1", True), ("2", False)])
def test_resume_enforces_persisted_toolset_manifest(
    tmp_path,
    monkeypatch,
    current_version,
    succeeds,
) -> None:
    from mote.kernel.tools.toolset import NativeToolset
    from mote.runtime.errors import SessionResumeIdentityError
    from mote.runtime.session.log import SessionLog

    monkeypatch.setattr("mote.runtime.session.log._default_base_dir", lambda: tmp_path)
    recorded = NativeToolset("workspace", (), version="1")
    session_id = "durable-toolsets"
    SessionLog(session_id, base_dir=str(tmp_path)).commit_offline(
        SessionMetaEvent(
            session_id=session_id,
            toolset_manifest=(recorded.identity,),
        )
    )
    current = NativeToolset("workspace", (), version=current_version)
    role = Role(name="Any", wiring=_offline_wiring(toolsets=(current,)))
    role.state.session_id = session_id

    if succeeds:
        assert role.resume_session() is True
    else:
        with pytest.raises(SessionResumeIdentityError):
            role.resume_session()


@pytest.mark.asyncio
async def test_resume_does_not_re_record_replayed_history(tmp_path, monkeypatch):
    from mote.runtime.models.clients.context import Context

    monkeypatch.setattr("mote.runtime.session.log._default_base_dir", lambda: tmp_path)

    role_a = Role(name="A", wiring=_offline_wiring())
    await _initialize_session_log(role_a)
    sid = role_a.session_id
    await _add_message(role_a, UserMessage(content="one"))

    role_b = Role(name="B", wiring=_offline_wiring())
    role_b.state.session_id = sid
    await role_b._components.start_event_fabric()
    role_b.resume_session()
    # A new live message after resume appends exactly once; replayed history is
    # not re-recorded (assigned straight into the backing context).
    await _add_message(role_b, UserMessage(content="two"))

    from mote.runtime.session.log import SessionLog

    messages = [
        event.message
        for envelope in SessionLog(sid, base_dir=str(tmp_path)).iter_events()
        if isinstance((event := decode_session_event(envelope)), MessageEvent)
    ]
    assert [message.content for message in messages] == ["one", "two"]


@pytest.mark.asyncio
async def test_resume_rebuilds_resource_registry(tmp_path, monkeypatch):
    from mote.runtime.models.clients.context import Context

    monkeypatch.setattr("mote.runtime.session.log._default_base_dir", lambda: tmp_path)

    # Session A records a sticky resource message (carries its id/kind/body in
    # metadata — the subclass identity is lost on dump/load, the metadata isn't).
    role_a = Role(name="A", wiring=_offline_wiring())
    await _initialize_session_log(role_a)
    sid = role_a.session_id
    await _add_message(
        role_a,
        ResourceMessage("SKILL BODY HERE", resource_id="deploy", resource_kind="skill"),
    )

    # Resume as a fresh role -> registry re-seeded from the replayed metadata.
    role_b = Role(name="B", wiring=_offline_wiring())
    role_b.state.session_id = sid
    assert role_b.resume_session() is True
    registry = role_b.resource_registry
    assert "deploy" in registry
    projected = registry.project(model="gpt-4")
    assert len(projected) == 1
    assert "SKILL BODY HERE" in projected[0].content


@pytest.mark.asyncio
async def test_resume_rebuilds_task_result_pointer_with_kind(tmp_path, monkeypatch):
    from mote.contracts.constants.messages import RESOURCE_KIND
    from mote.runtime.models.clients.context import Context

    monkeypatch.setattr("mote.runtime.session.log._default_base_dir", lambda: tmp_path)

    # A push-once bg-task pointer rides the SAME sticky-resource seam: it is
    # recorded as a task_result ResourceMessage, so resume must rebuild it under
    # kind="task_result" (not the "skill" default) so per-kind budgeting / round
    # reaping continue to apply after a restart.
    role_a = Role(name="A", wiring=_offline_wiring())
    await _initialize_session_log(role_a)
    sid = role_a.session_id
    await _add_message(
        role_a,
        ResourceMessage(
            "<task-result><task-id>bg_3</task-id></task-result>",
            resource_id="bg_3",
            resource_kind="task_result",
        ),
    )

    role_b = Role(name="B", wiring=_offline_wiring())
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
    from mote.runtime.models.clients.context import Context

    monkeypatch.setattr("mote.runtime.session.log._default_base_dir", lambda: tmp_path)

    role_a = Role(name="A", wiring=_offline_wiring())
    await _initialize_session_log(role_a)
    sid = role_a.session_id
    await _add_message(role_a, UserMessage(content="plain history, no resource"))

    role_b = Role(name="B", wiring=_offline_wiring())
    role_b.state.session_id = sid
    role_b.resume_session()
    # No resource markers in history -> registry stays empty.
    assert len(role_b.resource_registry) == 0
