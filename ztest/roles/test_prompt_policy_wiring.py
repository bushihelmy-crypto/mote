"""PromptPolicy is a lazy, per-Role component with per-incarnation extensions."""

from __future__ import annotations

from dataclasses import replace

from mote.contracts.conversation.prompt_policy import PromptPolicyContribution
from mote.contracts.ports.conversation.prompt_policy import PromptPolicyExtensionSpec
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

    dependencies = replace(
        AgentWiring.defaults().dependencies,
        prompt_policy_extensions=(PromptPolicyExtensionSpec("organization", factory),),
    )
    first = Role(
        name="first",
        wiring=AgentWiring.for_context(Context(), dependencies=dependencies),
    )
    second = Role(
        name="second",
        wiring=AgentWiring.for_context(Context(), dependencies=dependencies),
    )

    assert created == []
    assert first.prompt_policy is first.prompt_policy
    assert second.prompt_policy is second.prompt_policy
    assert len(created) == 2
    assert created[0] is not created[1]
