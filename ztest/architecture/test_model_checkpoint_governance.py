from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import cast

import pytest

from mote.contracts.execution.models import InferenceCheckpointAttemptState, InferenceCheckpointState
from mote.contracts.model import AttemptBudget, ModelCallFinishedRecord, ModelCallPlannedRecord, ModelCallState
from mote.contracts.model.checkpoint import require_narrower_attempt_budget
from mote.contracts.model.invocation import GenerateOutput
from mote.product.config.model_checkpoint import approved_model_checkpoint_policy
from mote.product.models.artifacts import ProductInferenceArtifacts
from mote.runtime.models.checkpoint_maintenance import (
    ModelCheckpointMaintenance,
    ModelCheckpointMaintenanceAction,
    ModelCheckpointMaintenanceCommand,
    ModelCheckpointMaintenanceDisposition,
)
from mote.runtime.models.failover import FailoverPlanner, LocalModelCallJournal, ModelCallCapacityError
from mote.runtime.models.model_gateway import RuntimeModelGateway
from mote.runtime.models.session_projection import ModelSessionProjectionStore
from mote.runtime.session.workspace import SessionWorkspace


def _plan(call_id: str, budget: AttemptBudget = AttemptBudget()) -> ModelCallPlannedRecord:
    return ModelCallPlannedRecord(
        model_call_id=call_id,
        plan_id=f"plan-{call_id}",
        route_id="default",
        runtime_generation_id="runtime-generation",
        topology_revision="topology",
        config_revision="config",
        endpoint_ids=("endpoint",),
        budget=budget,
    )


def test_product_policy_enforces_session_and_global_active_capacity(tmp_path) -> None:
    policy = replace(approved_model_checkpoint_policy(), active_per_session=1, active_global=1)
    session = ModelSessionProjectionStore("session", SessionWorkspace(tmp_path), policy)
    session.begin(InferenceCheckpointState("call-1"))
    with pytest.raises(RuntimeError, match="Session capacity"):
        session.begin(InferenceCheckpointState("call-2"))

    journal = LocalModelCallJournal(tmp_path / "model-calls", policy=policy)
    asyncio.run(journal.append(_plan("call-1")))
    with pytest.raises(ModelCallCapacityError) as raised:
        asyncio.run(journal.append(_plan("call-2")))
    assert raised.value.disposition.value == "limit_exceeded"


def test_checkpoint_schema_and_attempt_budget_fail_closed() -> None:
    with pytest.raises(ValueError, match="unsupported inference checkpoint schema"):
        InferenceCheckpointState("call", schema_version=2)
    with pytest.raises(ValueError, match="attempt state"):
        InferenceCheckpointState("call", attempt_state="wire_started")
    assert (
        InferenceCheckpointState("call", attempt_state=InferenceCheckpointAttemptState.IN_DOUBT).attempt_state
        is InferenceCheckpointAttemptState.IN_DOUBT
    )

    approved = AttemptBudget(
        max_wire_attempts=3,
        max_attempts_per_endpoint=3,
        max_endpoint_switches=2,
        max_credential_rotations=2,
        max_request_transforms=2,
    )
    assert (
        require_narrower_attempt_budget(
            approved,
            approved.model_copy(update={"max_wire_attempts": 2, "max_attempts_per_endpoint": 2}),
        ).max_wire_attempts
        == 2
    )
    with pytest.raises(ValueError, match="only be narrowed"):
        require_narrower_attempt_budget(
            approved,
            approved.model_copy(update={"max_wire_attempts": 4}),
        )


@pytest.mark.asyncio
async def test_oversized_model_response_is_externalized_before_terminal_journal(tmp_path) -> None:
    artifacts = ProductInferenceArtifacts(tmp_path)
    gateway = RuntimeModelGateway(
        cast(FailoverPlanner, object()),
        response_artifact_publisher=artifacts.publish,
    )

    inline = await gateway._journal_output(GenerateOutput(content="x" * (64 * 1024)))
    externalized = await gateway._journal_output(GenerateOutput(content="x" * (64 * 1024 + 1)))

    assert isinstance(inline, GenerateOutput) and inline.content_artifact is None
    assert isinstance(externalized, GenerateOutput)
    assert externalized.content == "" and externalized.content_artifact is not None
    assert externalized.content_artifact.size == 64 * 1024 + 1


def test_terminal_compaction_and_tombstone_purge_are_fenced_and_retained(tmp_path) -> None:
    policy = approved_model_checkpoint_policy()
    journal = LocalModelCallJournal(tmp_path / "model-calls", policy=policy)
    terminal_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    asyncio.run(journal.append(_plan("call")))
    asyncio.run(
        journal.append(
            ModelCallFinishedRecord(
                model_call_id="call",
                state=ModelCallState.SUCCEEDED,
                selected_endpoint_id="endpoint",
                occurred_at=terminal_at,
            )
        )
    )
    maintenance = ModelCheckpointMaintenance(tmp_path / "model-calls", policy=policy, fencing_token=7)

    stale = maintenance.execute(
        ModelCheckpointMaintenanceCommand(
            "stale",
            ModelCheckpointMaintenanceAction.COMPACT_TERMINAL,
            "call",
            "product-maintenance",
            6,
            terminal_at + timedelta(days=90),
        )
    )
    assert stale.disposition is ModelCheckpointMaintenanceDisposition.FENCED
    compacted = maintenance.execute(
        replace(
            ModelCheckpointMaintenanceCommand(
                "compact",
                ModelCheckpointMaintenanceAction.COMPACT_TERMINAL,
                "call",
                "product-maintenance",
                7,
                terminal_at + timedelta(days=90),
            )
        )
    )
    assert compacted.disposition is ModelCheckpointMaintenanceDisposition.APPLIED
    assert not journal.path_for("call").exists()

    early_command = ModelCheckpointMaintenanceCommand(
        "early",
        ModelCheckpointMaintenanceAction.PURGE_TOMBSTONE,
        "call",
        "product-maintenance",
        7,
        terminal_at + timedelta(days=364),
    )
    early = maintenance.execute(early_command)
    assert early.disposition is ModelCheckpointMaintenanceDisposition.NOT_ELIGIBLE
    purged = maintenance.execute(replace(early_command, command_id="purge", now=terminal_at + timedelta(days=365)))
    assert purged.disposition is ModelCheckpointMaintenanceDisposition.APPLIED
