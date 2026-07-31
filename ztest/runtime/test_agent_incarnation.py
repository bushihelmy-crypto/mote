"""Tests for in-process Agent replacement blueprints."""

from __future__ import annotations

import pytest

from mote.contracts.conversation import UserMessage
from mote.kernel.output import text_output_contract
from mote.orchestration.agents.control import AgentControl
from mote.orchestration.agents.lifecycle.runtime import AgentRuntime, AgentStatus
from mote.orchestration.agents.residency.store import ResidencyStore
from mote.runtime.agent import AgentDependencies, AgentWiring, Role
from mote.runtime.agent.incarnation import AgentIncarnationError, AgentIncarnationFactory
from mote.runtime.tools.provider import NativeToolset


def _role(*, version: str = "1", runtime_root=None) -> Role:
    dependencies = AgentDependencies(
        deps={"tenant": "acme"},
        output_contract=text_output_contract(),
        toolsets=(NativeToolset("workspace", (), version=version),),
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
        dependencies = AgentDependencies(
            deps={"tenant": "acme"},
            output_contract=text_output_contract(),
            toolsets=(NativeToolset("workspace", (), version=version),),
            user_config_root=paths.user_config_root,
            session_workspace_root=paths.session_workspace_root,
            browser_profiles_root=paths.browser_profiles_root,
            sandbox_ca_root=paths.sandbox_ca_root,
            secrets_root=paths.secrets_root,
            oauth_root=paths.oauth_root,
        )
        wiring = AgentWiring.for_context(Context(config=offline_config()), dependencies=dependencies)
    return Role(
        name="Worker",
        wiring=wiring,
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


def test_control_owns_blueprint_for_registered_agent_lifetime(tmp_path) -> None:
    original = _role(runtime_root=tmp_path)
    factory = AgentIncarnationFactory()
    control = AgentControl(
        store=ResidencyStore(
            base_dir=str(tmp_path / "residency"),
            sessions_base_dir=str(tmp_path / "sessions"),
        ),
        incarnation_factory=factory,
    )

    control.add_agent(AgentRuntime(original), root=True)

    assert factory.has(original.session_id) is True
    control.release_child(original.session_id)
    assert factory.has(original.session_id) is False


@pytest.mark.asyncio
async def test_residency_replacement_keeps_wiring_and_control(tmp_path) -> None:
    original = _role(runtime_root=tmp_path)
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
