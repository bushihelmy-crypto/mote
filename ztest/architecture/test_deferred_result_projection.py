from __future__ import annotations

from pathlib import Path

from mote.contracts.async_work import DurableWorkflowRunReference, DurableWorkflowRunSubmission
from mote.contracts.ports.tool.deferred import DeferredResultKind
from mote.contracts.task.lifecycle import BackgroundTaskAcceptance, BackgroundTaskOwner, LocalTaskReference
from mote.contracts.task.models import AttemptId, TaskId
from mote.contracts.workflow import WorkflowDefinitionId, WorkflowRunId, WorkflowRunReference
from mote.orchestration.background_tasks.model import BgTaskResult
from mote.orchestration.workflows.deferred import WorkflowDeferredResult, WorkflowRunMetadata
from mote.product.agents.deferred_projection import ProductDeferredResultProjector


def _projector() -> ProductDeferredResultProjector:
    return object.__new__(ProductDeferredResultProjector)


def test_product_adapter_classifies_authoritative_domain_types() -> None:
    projector = _projector()
    assert projector.classify(BgTaskResult.foreground("local")) is DeferredResultKind.BACKGROUND_TASK
    assert (
        projector.classify(
            WorkflowDeferredResult.hybrid(
                "durable",
                lambda: _poll(),
                command_name="workflow",
                graph_meta=WorkflowRunMetadata(),
            )
        )
        is DeferredResultKind.WORKFLOW
    )
    assert projector.classify(object()) is None


class _Service:
    def __init__(self) -> None:
        self.calls: list[tuple[object, str, dict[str, object]]] = []

    def submit(self, operation: object, command_name: str, **options: object) -> BackgroundTaskAcceptance:
        self.calls.append((operation, command_name, options))
        return BackgroundTaskAcceptance(
            LocalTaskReference(
                BackgroundTaskOwner("process", "agent", "incarnation"),
                TaskId(f"task-{len(self.calls)}"),
                AttemptId(1),
            )
        )


class _WorkflowService:
    def __init__(self) -> None:
        self.calls: list[WorkflowDeferredResult] = []
        self.observed = []

    def submit(self, deferred: WorkflowDeferredResult) -> DurableWorkflowRunSubmission:
        self.calls.append(deferred)
        return DurableWorkflowRunSubmission(
            DurableWorkflowRunReference(
                WorkflowRunReference(
                    WorkflowRunId("workflow-run-1"),
                    WorkflowDefinitionId("workflow-definition-1"),
                )
            ),
            1,
        )

    def observe(self, reference):
        self.observed.append(reference)


def test_product_adapter_keeps_background_and_workflow_submission_distinct() -> None:
    service = _Service()
    workflow_service = _WorkflowService()
    projector = _projector()
    projector._service = service  # type: ignore[assignment]
    projector._workflow_service = workflow_service  # type: ignore[assignment]
    projector._workflow_token = None

    background = projector.settle(
        BgTaskResult.background(lambda: _poll(), command_name="local"),
        tool_name="fallback",
    )
    metadata = WorkflowRunMetadata()
    workflow = projector.settle(
        WorkflowDeferredResult.background(
            lambda: _poll(),
            command_name="durable",
            graph_meta=metadata,
        ),
        tool_name="fallback",
    )

    assert background.kind is DeferredResultKind.BACKGROUND_TASK
    assert workflow.kind is DeferredResultKind.WORKFLOW
    assert service.calls[0][2] == {"progress": True}
    assert len(service.calls) == 1
    assert workflow_service.calls == [workflow.execution_value]
    assert background.submission is not None
    assert workflow.submission is not None
    assert workflow_service.observed == [
        background.submission.reference,
        workflow.submission.reference,
    ]


async def _poll() -> str:
    return "done"


def test_runtime_has_no_orchestration_reflection_or_magic_methods() -> None:
    pipeline = Path("runtime/tools/tool_pipeline.py").read_text(encoding="utf-8")
    assert "is_background_result" not in pipeline
    assert "to_tool_result" not in pipeline
    assert "orchestration" not in pipeline
    assert "projector.classify(raw)" in pipeline
    assert "projector.settle(raw" in pipeline


def test_workflow_aliases_and_upward_runtime_imports_are_retired() -> None:
    deferred = Path("orchestration/workflows/deferred.py").read_text(encoding="utf-8")
    background = Path("orchestration/background_tasks/model.py").read_text(encoding="utf-8")
    assert "BgTaskResult = WorkflowDeferredResult" not in deferred
    assert "GraphMeta = WorkflowRunMetadata" not in deferred
    assert "mote.runtime" not in deferred
    assert "mote.runtime" not in background


def test_product_composition_injects_the_only_projector_factory() -> None:
    wiring = Path("runtime/agent/wiring.py").read_text(encoding="utf-8")
    product = Path("product/agents/factory.py").read_text(encoding="utf-8")
    action = Path("runtime/agent/components/action.py").read_text(encoding="utf-8")
    assert "deferred_result_projector_factory" not in wiring
    assert "deferred_result_projector_factory:" in action
    assert "deferred_result_projector_factory=self._deferred_result_projector_factory" in product
    assert "projector_factory(" in action
    assert "ctx.dep(BACKGROUND_POOL)" in action
    assert "ctx.dep(ARTIFACT_PUBLISHER)" in action
    assert "_RoleWorkflowNodeExecution(ctx)" in action


def test_workflow_execution_does_not_enter_background_task_pool() -> None:
    projector = Path("product/agents/deferred_projection.py").read_text(encoding="utf-8")
    local_service = Path("product/agents/background_tasks.py").read_text(encoding="utf-8")
    workflow_service = Path("product/workflows/agent_service.py").read_text(encoding="utf-8")
    resume = Path("product/workflows/run_graph/resume_tasks.py").read_text(encoding="utf-8")
    inspect = Path("product/workflows/run_graph/get_node_state.py").read_text(encoding="utf-8")
    workflow_branch = projector.split("elif kind is DeferredResultKind.WORKFLOW:", 1)[1]
    assert "value.poll_factory," not in workflow_branch
    assert "pool.resubmit(task_id, bg_result.poll_factory" not in resume
    assert "get_bg_pool" not in resume
    assert "get_bg_pool" not in inspect
    assert "Workflow" not in local_service
    assert "reference.run_id, operation.execute, operation.aclose" in workflow_service


def test_background_pool_cannot_store_workflow_recovery_state() -> None:
    pool = Path("orchestration/background_tasks/pool.py").read_text(encoding="utf-8")
    model = Path("orchestration/background_tasks/model.py").read_text(encoding="utf-8")
    forbidden = (
        "WorkflowDeferredResult",
        "WorkflowRunMetadata",
        "WorkflowDefinition",
        "graph_meta",
        "checkpoint_payload",
        "continuation",
    )
    for symbol in forbidden:
        assert symbol not in pool
        assert symbol not in model


def test_workflow_tools_expose_run_identity_not_background_task_identity() -> None:
    for path in (
        Path("product/workflows/run_graph/resume_tasks.py"),
        Path("product/workflows/run_graph/get_node_state.py"),
    ):
        source = path.read_text(encoding="utf-8")
        assert "task_id" not in source
        assert "run_id" in source
