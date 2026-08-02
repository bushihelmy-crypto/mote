"""Composition-root coverage for ToolCallPolicy extensions."""

from __future__ import annotations

from mote.contracts.authorization import PermissionFacts
from mote.contracts.ports.tool.policy import ToolCallPolicyExtensionSpec
from mote.contracts.tool.policy import ToolCallInspection, ToolCallIntent
from mote.kernel.output import text_output_contract
from mote.product.agents.factory import CodingAgentFactory
from mote.runtime.agent import AgentWiring, Role
from mote.runtime.tools.permission import ToolCallInspector


class _DeploymentGate(ToolCallInspector):
    async def inspect(
        self,
        intent: ToolCallIntent,
        facts: PermissionFacts,
    ) -> ToolCallInspection:
        return ToolCallInspection.allow()


def test_role_seals_declared_tool_policy_extension_manifest() -> None:
    built: list[_DeploymentGate] = []

    def build() -> _DeploymentGate:
        extension = _DeploymentGate()
        built.append(extension)
        return extension

    spec = ToolCallPolicyExtensionSpec("deployment-gate", build)
    dependencies = CodingAgentFactory(
        tool_policy_extensions=(spec,),
    ).dependencies(deps=None, output_contract=text_output_contract())
    first = Role(wiring=AgentWiring.for_dependencies(dependencies))
    second = Role(wiring=AgentWiring.for_dependencies(dependencies))

    assert first.tool_call_policy.manifest == (spec,)
    assert second.tool_call_policy.manifest == (spec,)
    assert len(built) == 2
    assert built[0] is not built[1]
