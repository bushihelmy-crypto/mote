from __future__ import annotations

from pathlib import Path
from typing import get_args

from mote.contracts.async_work.observation import (
    AsyncWorkObservation,
    DurableWorkflowRunObservation,
    LocalBackgroundTaskObservation,
)
from mote.contracts.workflow.result import WorkflowTerminalOutcome


def _sources(root: str) -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in Path(root).rglob("*.py"))


def test_async_work_union_and_workflow_terminal_union_are_closed() -> None:
    assert set(get_args(AsyncWorkObservation)) == {
        LocalBackgroundTaskObservation,
        DurableWorkflowRunObservation,
    }
    terminal_names = {variant.__name__ for variant in get_args(WorkflowTerminalOutcome)}
    assert "WorkflowPaused" not in terminal_names
    assert "WorkflowInDoubt" not in terminal_names


def test_workflow_production_surface_has_no_background_task_identity() -> None:
    sources = _sources("orchestration/workflows") + _sources("product/workflows")
    for forbidden in (
        "task_id",
        "get_bg_pool",
        "from mote.orchestration.background_tasks",
        "StoredTaskOutput",
        "task-output:",
    ):
        assert forbidden not in sources


def test_old_async_work_control_and_event_chains_are_absent() -> None:
    sources = "\n".join(_sources(root) for root in ("contracts", "orchestration", "product", "runtime"))
    for forbidden in (
        "WorkflowProgressEvent",
        "WorkflowInspectionPort",
        "WorkflowBackgroundPort",
        "submit_workflow_result",
        "resume_workflow_result",
        "workflow_snapshot",
        "tool-result+json@3",
        "mote.workflow-checkpoint/v1",
        "mote.agent-lineage/v2",
    ):
        assert forbidden not in sources
    task_event = Path("contracts/events/task.py").read_text(encoding="utf-8")
    assert "def task_id" not in task_event


def test_product_surfaces_depend_on_contract_events_not_domain_owners() -> None:
    sources = "\n".join(
        _sources(root)
        for root in (
            "product/interfaces/acp",
            "product/interfaces/agui",
            "product/interfaces/terminal",
            "product/interfaces/textual",
            "product/presentation",
        )
    )
    assert "mote.orchestration.workflows" not in sources
    assert "mote.orchestration.background_tasks" not in sources
    assert "mote.product.workflows.durability" not in sources


def test_product_agents_do_not_use_concrete_workflow_durability_as_contract() -> None:
    assert "ProductWorkflowDurability" not in _sources("product/agents")
    service = Path("product/async_work/service.py").read_text(encoding="utf-8")
    for forbidden in ("def list", "BackgroundTaskPool", "WorkflowRunStore", "registry"):
        assert forbidden not in service
