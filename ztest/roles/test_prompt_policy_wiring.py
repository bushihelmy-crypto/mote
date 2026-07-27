"""PromptPolicy is a lazy, per-Role component with per-incarnation extensions."""

from __future__ import annotations

from mote.contracts.policy.prompt import PromptPolicyContribution
from mote.contracts.ports.prompt_policy import PromptPolicyExtensionSpec
from mote.kernel.output import text_output_contract
from mote.runtime.agent import AgentDependencies, AgentWiring, Role
from mote.runtime.models.clients.context import Context
from mote.ztest.model_fakes import offline_config


def test_prompt_policy_extension_factory_runs_once_per_role():
    created: list[object] = []

    class Extension:
        async def evaluate(self, intent):
            return PromptPolicyContribution()

    def factory():
        extension = Extension()
        created.append(extension)
        return extension

    dependencies = AgentDependencies(
        deps=None,
        output_contract=text_output_contract(),
        prompt_policy_extensions=(PromptPolicyExtensionSpec("organization", factory),),
    )
    first = Role(
        name="first",
        wiring=AgentWiring.for_context(Context(config=offline_config()), dependencies=dependencies),
    )
    second = Role(
        name="second",
        wiring=AgentWiring.for_context(Context(config=offline_config()), dependencies=dependencies),
    )

    assert created == []
    assert first.prompt_policy is first.prompt_policy
    assert second.prompt_policy is second.prompt_policy
    assert len(created) == 2
    assert created[0] is not created[1]
