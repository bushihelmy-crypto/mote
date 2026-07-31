from __future__ import annotations

from mote.contracts.tool.execution import ToolExecutionKind
from mote.runtime.tools.base_tool import BaseTool
from mote.runtime.tools.definitions import native_definition, xml_definition


class DeferredWorkflowTool(BaseTool):
    name = "DeferredWorkflow"
    aliases = ["workflow_alias"]
    execution_kind = ToolExecutionKind.WORKFLOW_DEFERRED

    async def call(self):
        return "ok"


def test_execution_kind_survives_definition_transforms():
    for definition in (
        native_definition(DeferredWorkflowTool),
        xml_definition(DeferredWorkflowTool),
    ):
        assert definition.execution_kind is ToolExecutionKind.WORKFLOW_DEFERRED
        assert definition.renamed("renamed").execution_kind is ToolExecutionKind.WORKFLOW_DEFERRED
        assert definition.prefixed("namespace").execution_kind is ToolExecutionKind.WORKFLOW_DEFERRED
