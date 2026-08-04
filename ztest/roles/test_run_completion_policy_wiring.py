"""RunCompletionPolicy is lazy and extensions are isolated per Role."""

from __future__ import annotations

from mote.contracts.output.policy import RunCompletionPolicyContribution
from mote.contracts.ports.output.run_completion_policy import RunCompletionPolicyExtensionSpec
from mote.kernel.output import text_output_contract
from mote.product.agents.factory import CodingAgentFactory
from mote.product.config.model_checkpoint import approved_model_checkpoint_policy
from mote.runtime.agent import AgentWiring, Role
from mote.runtime.models.clients.context import Context
from mote.ztest.model_fakes import offline_config


def test_run_completion_policy_extension_factory_runs_once_per_role():
    created: list[object] = []

    class Extension:
        async def evaluate(self, intent):
            return RunCompletionPolicyContribution()

    def factory():
        extension = Extension()
        created.append(extension)
        return extension

    dependencies = CodingAgentFactory(
        model_checkpoint_policy=approved_model_checkpoint_policy(),
        run_completion_policy_extensions=(RunCompletionPolicyExtensionSpec("organization", factory),),
    ).dependencies(deps=None, output_contract=text_output_contract())
    first = Role(
        name="first",
        wiring=AgentWiring.for_context(Context(), dependencies=dependencies),
    )
    second = Role(
        name="second",
        wiring=AgentWiring.for_context(Context(), dependencies=dependencies),
    )

    assert created == []
    assert first.run_completion_policy is first.run_completion_policy
    assert second.run_completion_policy is second.run_completion_policy
    assert len(created) == 2
    assert created[0] is not created[1]
