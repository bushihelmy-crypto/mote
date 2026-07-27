"""CompactionPolicy is lazy and extensions are isolated per Role."""

from __future__ import annotations

from mote.contracts.policy.compaction import CompactionPolicyContribution
from mote.contracts.ports.compaction_policy import CompactionPolicyExtensionSpec
from mote.kernel.output import text_output_contract
from mote.runtime.agent import AgentDependencies, AgentWiring, Role
from mote.runtime.models.clients.context import Context
from mote.runtime.session import log as session_log_module
from mote.ztest.model_fakes import FakeModelGateway, offline_config

from .conftest import FakeLLM


def _context() -> Context:
    llm = FakeLLM()
    context = Context(
        config=offline_config(),
        provider_factory=lambda config: FakeLLM(name=config.model),
    )
    context.model_gateway = FakeModelGateway(llm)
    return context


def test_compaction_policy_extension_factory_runs_once_per_role(tmp_path, monkeypatch):
    monkeypatch.setattr(
        session_log_module,
        "_default_base_dir",
        lambda: tmp_path / "workspace" / "sessions",
    )
    created: list[object] = []

    class Extension:
        async def evaluate(self, intent):
            return CompactionPolicyContribution()

    def factory():
        extension = Extension()
        created.append(extension)
        return extension

    dependencies = AgentDependencies(
        deps=None,
        output_contract=text_output_contract(),
        compaction_policy_extensions=(CompactionPolicyExtensionSpec("organization", factory),),
    )
    first = Role(
        name="first",
        wiring=AgentWiring.for_context(
            _context(),
            dependencies=dependencies,
        ),
    )
    second = Role(
        name="second",
        wiring=AgentWiring.for_context(
            _context(),
            dependencies=dependencies,
        ),
    )

    assert created == []
    assert first.context_manager is first.context_manager
    assert second.context_manager is second.context_manager
    assert len(created) == 2
    assert created[0] is not created[1]
