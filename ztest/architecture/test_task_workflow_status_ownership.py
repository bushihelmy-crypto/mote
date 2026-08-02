from __future__ import annotations

from pathlib import Path

import pytest

from mote.orchestration.background_tasks.status import (
    BackgroundTaskStatus,
    decode_background_task_status,
    project_background_task_status,
)
from mote.orchestration.workflows.types import (
    WorkflowNodeStatus,
    decode_workflow_node_status,
    project_workflow_node_status,
)


def test_status_types_are_nominally_distinct() -> None:
    assert type(BackgroundTaskStatus.SUCCESS) is not type(WorkflowNodeStatus.SUCCESS)


def test_source_tagged_projection_round_trips_per_owner() -> None:
    background = project_background_task_status(BackgroundTaskStatus.TIMEOUT)
    workflow = project_workflow_node_status(WorkflowNodeStatus.SKIPPED)
    assert decode_background_task_status(background.to_payload()) is BackgroundTaskStatus.TIMEOUT
    assert decode_workflow_node_status(workflow.to_payload()) is WorkflowNodeStatus.SKIPPED


def test_cross_owner_status_decode_fails_closed() -> None:
    background = project_background_task_status(BackgroundTaskStatus.FAILED)
    workflow = project_workflow_node_status(WorkflowNodeStatus.FAILED)
    with pytest.raises(ValueError, match="source"):
        decode_workflow_node_status(background.to_payload())
    with pytest.raises(ValueError, match="source"):
        decode_background_task_status(workflow.to_payload())


def test_old_status_name_and_common_owner_residue_are_absent() -> None:
    production = "\n".join(
        path.read_text(encoding="utf-8")
        for root in (Path("orchestration"), Path("product"), Path("runtime"))
        for path in root.rglob("*.py")
    )
    assert "BgStatus" not in production
    assert "common/schema/node_status.py" not in production


def test_background_pool_does_not_own_workflow_resume_state() -> None:
    pool = Path("orchestration/background_tasks/pool.py").read_text(encoding="utf-8")
    for forbidden in (
        "WorkflowDefinition",
        "WorkflowContinuation",
        "WorkflowCheckpoint",
        "WorkflowRun",
    ):
        assert forbidden not in pool
