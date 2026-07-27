"""Tests for in-process Agent replacement blueprints."""

from __future__ import annotations

import pytest

from mote.contracts.schema import UserMessage
from mote.kernel.output import text_output_contract
from mote.kernel.tools.toolset import NativeToolset
from mote.orchestration.environment.control import AgentControl
from mote.orchestration.environment.runtime import AgentRuntime, AgentStatus
from mote.orchestration.environment.store import ResidencyStore
from mote.runtime.agent import AgentDependencies, AgentWiring, Role
from mote.runtime.agent.incarnation import AgentIncarnationError, AgentIncarnationFactory


def _role(*, version: str = "1") -> Role:
    dependencies = AgentDependencies(
        deps={"tenant": "acme"},
        output_contract=text_output_contract(),
        toolsets=(NativeToolset("workspace", (), version=version),),
    )
    return Role(
        name="Worker",
        wiring=AgentWiring.for_dependencies(dependencies),
    )


def test_blueprint_restores_state_with_original_atomic_wiring() -> None:
    original = _role()
    original.state.working_dir = "/workspace/original"
    snapshot = original.dump()
    snapshot["state"]["working_dir"] = "/workspace/restored"
    factory = AgentIncarnationFactory()
    factory.register(original.session_id, original.incarnation_blueprint())

    restored = factory.restore(original.session_id, snapshot)

    assert isinstance(restored, Role)
    assert restored is not original
    assert restored.state.working_dir == "/workspace/restored"
    assert restored.wiring is original.wiring
    assert restored.wiring.dependencies is original.wiring.dependencies
    restored.validate_resume_identity(
        {"toolset_manifest": [original.wiring.dependencies.toolsets[0].identity.to_payload()]}
    )


def test_factory_loader_is_store_compatible() -> None:
    original = _role()
    factory = AgentIncarnationFactory()
    factory.register(original.session_id, original.incarnation_blueprint())

    restored = factory.loader(original.session_id)(original.dump())

    assert isinstance(restored, Role)
    assert restored.session_id == original.session_id


def test_missing_blueprint_fails_closed() -> None:
    factory = AgentIncarnationFactory()

    with pytest.raises(AgentIncarnationError, match="not cross-process"):
        factory.loader("missing")({})


def test_snapshot_role_type_mismatch_fails_closed() -> None:
    original = _role()
    factory = AgentIncarnationFactory()
    factory.register(original.session_id, original.incarnation_blueprint())
    snapshot = original.dump()
    snapshot["type_id"] = "mote.other-role.v1"

    with pytest.raises(AgentIncarnationError, match="snapshot type"):
        factory.restore(original.session_id, snapshot)


def test_unregister_removes_construction_authority() -> None:
    original = _role()
    factory = AgentIncarnationFactory()
    factory.register(original.session_id, original.incarnation_blueprint())

    factory.unregister(original.session_id)

    assert factory.has(original.session_id) is False
    with pytest.raises(AgentIncarnationError):
        factory.restore(original.session_id, original.dump())


def test_control_owns_blueprint_for_registered_agent_lifetime() -> None:
    original = _role()
    factory = AgentIncarnationFactory()
    control = AgentControl(incarnation_factory=factory)

    control.add_agent(AgentRuntime(original), root=True)

    assert factory.has(original.session_id) is True
    control.release_child(original.session_id)
    assert factory.has(original.session_id) is False


@pytest.mark.asyncio
async def test_residency_replacement_keeps_wiring_and_control(tmp_path) -> None:
    original = _role()
    runtime = AgentRuntime(original)
    runtime.status = AgentStatus.COMPLETED
    factory = AgentIncarnationFactory()
    store = ResidencyStore(
        base_dir=str(tmp_path / "residency"),
        sessions_base_dir=str(tmp_path / "sessions"),
    )
    control = AgentControl(
        store=store,
        residency_capacity=1,
        incarnation_factory=factory,
    )
    control.add_agent(runtime, root=True)

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
    assert restored.role.agent_control is control
    assert restored.mailbox.empty() is False
