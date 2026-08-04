"""Tests for in-process Agent replacement blueprints."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from mote.contracts.conversation import UserMessage
from mote.kernel.output import text_output_contract
from mote.orchestration.agents.control import AgentControl
from mote.orchestration.agents.lifecycle.runtime import AgentRuntime, AgentStatus
from mote.orchestration.agents.residency.store import ResidencyStore
from mote.product.agents.factory import CodingAgentFactory
from mote.product.config.model_checkpoint import approved_model_checkpoint_policy
from mote.runtime.agent import AgentDependencies, AgentWiring, Role
from mote.runtime.agent.incarnation import AgentIncarnationBlueprint, AgentIncarnationError, AgentIncarnationFactory
from mote.runtime.control.leases import InMemoryLeaseCoordinator
from mote.runtime.session import SessionLog
from mote.runtime.session.events import SessionMetaEvent
from mote.runtime.tools.provider import NativeToolset


def _role(*, version: str = "1", runtime_root=None) -> Role:
    toolsets = (NativeToolset("workspace", (), version=version),)
    factory = CodingAgentFactory(
        model_checkpoint_policy=approved_model_checkpoint_policy(),
    )
    dependencies = factory.dependencies(
        deps={"tenant": "acme"}, output_contract=text_output_contract(), toolsets=toolsets
    )
    wiring = AgentWiring.for_dependencies(dependencies)
    if runtime_root is not None:
        from mote.product.paths import default_runtime_paths
        from mote.runtime.models.clients.context import Context
        from mote.ztest.model_fakes import offline_config

        paths = default_runtime_paths(
            user_config_root=runtime_root / "config",
            workspace_root=runtime_root / "workspace",
        )
        dependencies = CodingAgentFactory(
            model_checkpoint_policy=approved_model_checkpoint_policy(), paths=paths
        ).dependencies(deps={"tenant": "acme"}, output_contract=text_output_contract(), toolsets=toolsets)
        wiring = AgentWiring.for_context(Context(), dependencies=dependencies)
    return Role(
        name="Worker",
        wiring=wiring,
    )


def test_blueprint_restores_state_with_original_atomic_wiring() -> None:
    original = _role()
    original.state.working_dir = "/workspace/restored"
    snapshot = original.export_residency_state(session_history_is_durable=False)
    factory = AgentIncarnationFactory()
    factory.register(original.session_id, original.incarnation_blueprint())

    restored = factory.restore(original.session_id, snapshot)

    assert isinstance(restored, Role)
    assert restored is not original
    assert restored.state.working_dir == "/workspace/restored"
    assert restored.wiring is original.wiring
    assert restored.wiring.dependencies is original.wiring.dependencies
    restored.validate_resume_identity(
        {
            "role_class": original.role_type_id,
            "toolset_manifest": [original.wiring.dependencies.toolsets[0].identity.to_payload()],
        }
    )


def test_factory_loader_is_store_compatible() -> None:
    original = _role()
    factory = AgentIncarnationFactory()
    factory.register(original.session_id, original.incarnation_blueprint())

    restored = factory.loader(original.session_id)(original.export_residency_state(session_history_is_durable=False))

    assert isinstance(restored, Role)
    assert restored.session_id == original.session_id


def test_missing_blueprint_fails_closed() -> None:
    factory = AgentIncarnationFactory()

    with pytest.raises(AgentIncarnationError, match="no in-process"):
        factory.loader("missing")({})


def test_trusted_blueprint_definition_mismatch_fails_closed() -> None:
    original = _role()
    canonical = original.incarnation_blueprint()
    mismatched = AgentIncarnationBlueprint(
        definition_id="mote.other-role.v1",
        config_digest=canonical.config_digest,
        restore=canonical.restore,
    )

    with pytest.raises(AgentIncarnationError, match="another Agent definition"):
        mismatched.build(original.export_residency_state(session_history_is_durable=False))


def test_unregister_removes_construction_authority() -> None:
    original = _role()
    factory = AgentIncarnationFactory()
    factory.register(original.session_id, original.incarnation_blueprint())

    factory.unregister(original.session_id)

    assert factory.has(original.session_id) is False
    with pytest.raises(AgentIncarnationError):
        factory.restore(
            original.session_id,
            original.export_residency_state(session_history_is_durable=False),
        )


@pytest.mark.asyncio
async def test_control_owns_blueprint_for_registered_agent_lifetime(tmp_path) -> None:
    original = _role(runtime_root=tmp_path)
    factory = AgentIncarnationFactory()
    leases = InMemoryLeaseCoordinator()
    control = AgentControl(
        store=ResidencyStore(
            base_dir=str(tmp_path / "residency"),
            sessions_base_dir=str(tmp_path / "sessions"),
            lease_coordinator=leases,
        ),
        incarnation_factory=factory,
        residency_lease_coordinator=leases,
        lineage_path=tmp_path / "agent-lineage.json",
        turn_queue_capacity=256,
    )

    control.add_agent(AgentRuntime(original), root=True)

    assert factory.has(original.session_id) is True
    await control.release_child(original.session_id)
    assert factory.has(original.session_id) is False


@pytest.mark.asyncio
async def test_residency_replacement_keeps_wiring_and_control(tmp_path) -> None:
    original = _role(runtime_root=tmp_path)
    runtime = AgentRuntime(original)
    runtime.status = AgentStatus.COMPLETED
    factory = AgentIncarnationFactory()
    leases = InMemoryLeaseCoordinator()
    store = ResidencyStore(
        base_dir=str(tmp_path / "residency"),
        sessions_base_dir=str(tmp_path / "sessions"),
        lease_coordinator=leases,
    )
    control = AgentControl(
        store=store,
        max_resident_incarnations=1,
        incarnation_factory=factory,
        residency_lease_coordinator=leases,
        lineage_path=tmp_path / "agent-lineage.json",
        turn_queue_capacity=256,
    )
    control.add_agent(runtime, root=True)
    SessionLog(original.session_id, base_dir=str(tmp_path / "sessions")).commit_offline(
        SessionMetaEvent(
            session_id=original.session_id,
            role_class=original.role_type_id or "",
            toolset_manifest=tuple(toolset.identity for toolset in original.wiring.dependencies.toolsets),
        )
    )

    replacement_slot = await control.residency.reserve_slot(1)
    replacement_slot.rollback()
    assert control.get_runtime(original.session_id) is None

    restored = control.send_input(
        original.session_id,
        UserMessage("wake"),
    )

    assert restored is not None
    assert restored.role is not original
    assert restored.role.wiring is original.wiring


@pytest.mark.asyncio
async def test_rehydrate_commit_failure_rolls_back_live_install_and_generation(tmp_path, monkeypatch) -> None:
    original = _role(runtime_root=tmp_path)
    runtime = AgentRuntime(original)
    runtime.status = AgentStatus.COMPLETED
    factory = AgentIncarnationFactory()
    leases = InMemoryLeaseCoordinator()
    store = ResidencyStore(
        base_dir=str(tmp_path / "residency"),
        sessions_base_dir=str(tmp_path / "sessions"),
        lease_coordinator=leases,
    )
    control = AgentControl(
        store=store,
        max_resident_incarnations=1,
        incarnation_factory=factory,
        residency_lease_coordinator=leases,
        lineage_path=tmp_path / "agent-lineage.json",
        turn_queue_capacity=256,
    )
    control.add_agent(runtime, root=True)
    SessionLog(original.session_id, base_dir=str(tmp_path / "sessions")).commit_offline(
        SessionMetaEvent(
            session_id=original.session_id,
            role_class=original.role_type_id or "",
            toolset_manifest=tuple(toolset.identity for toolset in original.wiring.dependencies.toolsets),
        )
    )
    replacement_slot = await control.residency.reserve_slot(1)
    replacement_slot.rollback()
    assert control.get_runtime(original.session_id) is None

    canonical_forget = store.forget

    def fail_forget(*args, **kwargs):
        raise RuntimeError("injected durable commit failure")

    monkeypatch.setattr(store, "forget", fail_forget)
    with pytest.raises(RuntimeError, match="injected durable commit failure"):
        control.send_input(original.session_id, UserMessage("wake"))

    assert control.get_runtime(original.session_id) is None
    assert factory.generation(original.session_id) == 1
    claimed = store.read_record(original.session_id)
    assert claimed is not None
    assert claimed.lifecycle.value == "installing"

    monkeypatch.setattr(store, "forget", canonical_forget)
    restored = control.send_input(original.session_id, UserMessage("retry"))
    assert restored is not None
    assert factory.generation(original.session_id) == 2
    assert not store.has(original.session_id)
    assert restored.mailbox.empty() is False
    assert factory.generation(original.session_id) == 2
    assert store.has(original.session_id) is False


@pytest.mark.asyncio
async def test_concurrent_rehydrate_constructs_and_commits_one_incarnation(tmp_path, monkeypatch) -> None:
    original = _role(runtime_root=tmp_path)
    runtime = AgentRuntime(original)
    runtime.status = AgentStatus.COMPLETED
    factory = AgentIncarnationFactory()
    leases = InMemoryLeaseCoordinator()
    store = ResidencyStore(
        base_dir=str(tmp_path / "residency"),
        sessions_base_dir=str(tmp_path / "sessions"),
        lease_coordinator=leases,
    )
    control = AgentControl(
        store=store,
        max_resident_incarnations=1,
        incarnation_factory=factory,
        residency_lease_coordinator=leases,
        lineage_path=tmp_path / "agent-lineage.json",
        turn_queue_capacity=256,
    )
    control.add_agent(runtime, root=True)
    SessionLog(original.session_id, base_dir=str(tmp_path / "sessions")).commit_offline(
        SessionMetaEvent(
            session_id=original.session_id,
            role_class=original.role_type_id or "",
            toolset_manifest=tuple(toolset.identity for toolset in original.wiring.dependencies.toolsets),
        )
    )
    replacement_slot = await control.residency.reserve_slot(1)
    replacement_slot.rollback()
    canonical_rehydrate = store.rehydrate
    started = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def blocked_rehydrate(*args, **kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
        started.set()
        assert release.wait(timeout=5)
        return canonical_rehydrate(*args, **kwargs)

    monkeypatch.setattr(store, "rehydrate", blocked_rehydrate)
    with ThreadPoolExecutor(max_workers=10) as executor:
        first = executor.submit(control.send_input, original.session_id, UserMessage("wake-0"))
        assert started.wait(timeout=5)
        rest = [
            executor.submit(
                control.send_input,
                original.session_id,
                UserMessage(f"wake-{index}"),
            )
            for index in range(1, 10)
        ]
        release.set()
        results = [first.result(timeout=5)]
        results.extend(item.result(timeout=5) for item in rest)
    assert calls == 1
    assert sum(result is not None for result in results) == 1
    assert control.get_runtime(original.session_id) is results[0]
