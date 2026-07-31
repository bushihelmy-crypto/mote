"""CompactionPolicy is lazy and extensions are isolated per Role."""

from __future__ import annotations

from dataclasses import replace

from mote.contracts.conversation.compaction_policy import CompactionPolicyContribution
from mote.contracts.ports.conversation.compaction_policy import CompactionPolicyExtensionSpec
from mote.kernel.output import text_output_contract
from mote.runtime.agent import AgentDependencies, AgentWiring, Role
from mote.runtime.models.clients.context import Context
from mote.runtime.session import log as session_log_module
from mote.ztest.model_fakes import bind_fake_runtime, offline_config

from .conftest import FakeLLM


def _context() -> Context:
    llm = FakeLLM()
    return Context(config=offline_config())


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

    dependencies = replace(
        AgentWiring.defaults().dependencies,
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
    bind_fake_runtime(first, FakeLLM())
    bind_fake_runtime(second, FakeLLM())

    assert created == []
    assert first.context_manager is first.context_manager
    assert second.context_manager is second.context_manager
    assert len(created) == 2
    assert created[0] is not created[1]
