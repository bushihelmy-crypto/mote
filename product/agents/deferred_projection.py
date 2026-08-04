"""Product-owned adapter for Orchestration deferred result variants."""

from __future__ import annotations

from contextvars import Token

from mote.contracts.async_work import (
    DurableWorkflowRunSubmission,
    LocalBackgroundTaskReference,
    LocalBackgroundTaskSubmission,
)
from mote.contracts.ports.artifact.store import ReliableArtifactPublisher
from mote.contracts.ports.task.operations import BackgroundTaskService
from mote.contracts.ports.tool.deferred import DeferredResultKind, DeferredResultProjector, DeferredToolSettlement
from mote.contracts.ports.workflow.execution import WorkflowNodeExecutionPort
from mote.contracts.workflow import WorkflowRunReference
from mote.orchestration.background_tasks.model import (
    BackgroundBgTaskResult,
    BgTaskResult,
    ForegroundBgTaskResult,
    HybridBgTaskResult,
)
from mote.orchestration.workflows.deferred import WorkflowDeferredResult, WorkflowExecutionMode
from mote.product.agents.background_tasks import AgentBackgroundTasks
from mote.product.workflows.agent_context import bind_agent_workflows, reset_agent_workflows
from mote.product.workflows.agent_service import AgentWorkflowService

_MSG_BG_SUBMITTED = (
    "Background task '{name}' submitted{task_ref}. Running asynchronously — " "you will be notified when it completes."
)


class ProductDeferredResultProjector:
    def __init__(
        self,
        service: BackgroundTaskService,
        workflows: AgentWorkflowService | None,
    ) -> None:
        if not isinstance(service, AgentBackgroundTasks):
            raise TypeError("Product deferred projection requires AgentBackgroundTasks")
        self._service = service
        self._workflow_service = workflows
        self._workflow_token: Token[AgentWorkflowService | None] | None = None

    def activate(self) -> None:
        if self._workflow_token is not None:
            raise RuntimeError("Agent Workflow capability is already active")
        if self._workflow_service is not None:
            self._workflow_token = bind_agent_workflows(self._workflow_service)

    def deactivate(self) -> None:
        if self._workflow_token is not None:
            reset_agent_workflows(self._workflow_token)
            self._workflow_token = None

    def classify(self, value: object) -> DeferredResultKind | None:
        if isinstance(value, (ForegroundBgTaskResult, BackgroundBgTaskResult, HybridBgTaskResult)):
            return DeferredResultKind.BACKGROUND_TASK
        if isinstance(value, WorkflowDeferredResult):
            return DeferredResultKind.WORKFLOW
        return None

    def settle(
        self,
        value: object,
        *,
        tool_name: str,
    ) -> DeferredToolSettlement:
        kind = self.classify(value)
        submission = None
        if kind is DeferredResultKind.BACKGROUND_TASK:
            assert isinstance(value, (ForegroundBgTaskResult, BackgroundBgTaskResult, HybridBgTaskResult))
            task_id = None
            if isinstance(value, (BackgroundBgTaskResult, HybridBgTaskResult)):
                task_id = self._service.submit(
                    value.poll_factory,
                    value.command_name or tool_name,
                    progress=True,
                )
                submission = LocalBackgroundTaskSubmission(LocalBackgroundTaskReference(task_id.reference))
            output = self._output(
                isinstance(value, BackgroundBgTaskResult),
                None if isinstance(value, BackgroundBgTaskResult) else value.result,
                value.command_name or tool_name,
                task_id,
            )
        elif kind is DeferredResultKind.WORKFLOW:
            assert isinstance(value, WorkflowDeferredResult)
            if self._workflow_service is None:
                raise RuntimeError("Product Workflow durability is not activated")
            workflow_reference = None
            if value.mode in {
                WorkflowExecutionMode.BACKGROUND,
                WorkflowExecutionMode.HYBRID,
            }:
                workflow_submission = self._workflow_service.submit(value)
                workflow_reference = workflow_submission.reference.reference
                submission = workflow_submission
            output = self._output(
                value.mode is WorkflowExecutionMode.BACKGROUND,
                value.result,
                value.command_name or tool_name,
                workflow_reference,
            )
            if value.mode is WorkflowExecutionMode.BACKGROUND:
                summary = value.graph_meta.stage_summary if value.graph_meta is not None else ""
                if summary:
                    output = f"{output}\nstage-summary:\n{summary}"
        else:
            raise TypeError("value is not a supported deferred result")
        if submission is not None and self._workflow_service is not None:
            self._workflow_service.observe(submission.reference)
        return DeferredToolSettlement(kind, output, value, submission)

    async def aclose(self) -> None:
        self.deactivate()
        if self._workflow_service is not None:
            await self._workflow_service.aclose()

    @staticmethod
    def _output(
        background: bool,
        immediate: object,
        command_name: str,
        reference: str | WorkflowRunReference | None,
    ) -> str:
        if not background:
            return str(immediate) if immediate is not None else ""
        if isinstance(reference, WorkflowRunReference):
            task_ref = f" (workflow_run_id: {reference.run_id}, " f"workflow_definition_id: {reference.definition_id})"
        elif reference is not None:
            task_ref = f" (task_id: {reference})"
        else:
            task_ref = ""
        return _MSG_BG_SUBMITTED.format(name=command_name, task_ref=task_ref)


def build_deferred_result_projector(
    service: BackgroundTaskService,
    artifact_publisher: ReliableArtifactPublisher,
    workflow_nodes: WorkflowNodeExecutionPort,
    workflows: AgentWorkflowService | None = None,
) -> DeferredResultProjector:
    del artifact_publisher, workflow_nodes
    return ProductDeferredResultProjector(service, workflows)


__all__ = [
    "ProductDeferredResultProjector",
    "build_deferred_result_projector",
]
