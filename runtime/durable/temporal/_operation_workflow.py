"""Temporal-owned command state for one typed external operation."""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from mote.contracts.runtime.operation_ownership import EffectCapability
from mote.runtime.durable.temporal._activities import RUN_STEP_ACTIVITY, StepInput


@workflow.defn(name="mote__typed_operation_workflow_v1")
class TypedOperationWorkflow:
    @workflow.run
    async def run(self, command: StepInput) -> str:
        attempts = (
            3
            if command.effect_capability
            in {
                EffectCapability.NO_EXTERNAL_EFFECT,
                EffectCapability.IDEMPOTENT_BY_KEY,
            }
            else 1
        )
        return await workflow.execute_activity(
            RUN_STEP_ACTIVITY,
            command,
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=attempts),
        )


__all__ = ["TypedOperationWorkflow"]
