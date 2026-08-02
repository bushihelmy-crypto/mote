"""Model-facing Workflow cancellation through the bound async-work command path."""

from mote.contracts.tool.errors import ToolError
from mote.contracts.workflow import WorkflowDefinitionId, WorkflowRunId, WorkflowRunReference
from mote.product.workflows.agent_context import resolve_agent_workflows
from mote.runtime.tools.base_tool import BaseTool


class CancelWorkflowRun(BaseTool):
    name = "CancelWorkflowRun"

    async def call(
        self,
        *,
        run_id: str,
        definition_id: str,
        expected_revision: int,
    ) -> str:
        """Request cancellation of one durable Workflow at an observed revision."""
        reference = WorkflowRunReference(WorkflowRunId(run_id), WorkflowDefinitionId(definition_id))
        receipt = resolve_agent_workflows().cancel(reference, expected_revision=expected_revision)
        if receipt.revision is None:
            raise ToolError(f"Workflow cancellation rejected: {receipt.disposition.value}")
        return f"Workflow {run_id} cancellation is {receipt.disposition.value} " f"at revision {receipt.revision}."


__all__ = ["CancelWorkflowRun"]
