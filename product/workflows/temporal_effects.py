"""Opt-in Temporal worker/client lifecycle for Workflow external effects."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

from mote.contracts.config.tool import TemporalConfig
from mote.contracts.runtime.operation_ownership import EffectSettlement
from mote.orchestration.workflows.durable import WorkflowEffect
from mote.runtime.durable.temporal import (
    StepInput,
    TemporalActivityCatalog,
    TemporalBackend,
    build_worker,
    connect_client,
)
from mote.runtime.durable.temporal._operation_workflow import TypedOperationWorkflow
from mote.runtime.ledger import RunJournal
from mote.runtime.session.workspace.store import SessionWorkspace

_HANDLER_ID = "mote.workflow.effect-dispatch/v1"
EffectDispatch = Callable[[StepInput], Awaitable[str]]


class TemporalWorkflowEffects:
    def __init__(
        self,
        config: TemporalConfig,
        *,
        workspace: SessionWorkspace,
        dispatch: EffectDispatch,
    ) -> None:
        self._config = config
        catalog = TemporalActivityCatalog()
        catalog.register(_HANDLER_ID, dispatch)
        self._backend = TemporalBackend(
            config,
            RunJournal("application-workflow-effects", workspace),
            activity_catalog=catalog,
        )
        self._client = None
        self._worker = None
        self._worker_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._worker_task is not None:
            raise RuntimeError("Temporal Workflow effect plane is already active")
        self._client = await connect_client(self._config)
        self._worker = build_worker(
            self._client,
            self._backend,
            workflows=(TypedOperationWorkflow,),
        )
        self._worker_task = asyncio.create_task(self._worker.run(), name="mote-temporal-workflow-effects")

    async def execute(self, effect: WorkflowEffect) -> tuple[EffectSettlement, str]:
        if self._client is None:
            raise RuntimeError("Temporal Workflow effect plane is not active")
        command = StepInput(
            step_id=effect.effect_id,
            handler_id=_HANDLER_ID,
            payload=json.dumps(
                {
                    "schema": "mote.workflow-effect-command/v1",
                    "effect_id": effect.effect_id,
                    "run_id": effect.run_id,
                    "capability": effect.capability.value,
                    "command_payload": effect.command_payload,
                    "provider_receipt": effect.provider_receipt,
                    "state": effect.state.value,
                    "revision": effect.revision,
                    "attempts": effect.attempts,
                    "next_eligible_at": effect.next_eligible_at.to_dict(),
                    "reason": effect.reason,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            kind="workflow_effect",
            effect_id=effect.effect_id,
            effect_capability=effect.capability,
        )
        result = await self._client.execute_workflow(
            TypedOperationWorkflow.run,
            command,
            id=effect.effect_id,
            task_queue=self._config.task_queue,
        )
        raw = json.loads(result)
        if type(raw) is not dict or set(raw) != {"settlement", "receipt"}:
            raise ValueError("Temporal Workflow effect result is invalid")
        if type(raw["settlement"]) is not str or type(raw["receipt"]) is not str:
            raise ValueError("Temporal Workflow effect result primitives are invalid")
        return EffectSettlement(raw["settlement"]), raw["receipt"]

    async def aclose(self) -> None:
        task = self._worker_task
        self._worker_task = None
        if self._worker is not None:
            await self._worker.shutdown()
        if task is not None:
            await task
        self._worker = None
        self._client = None


__all__ = ["TemporalWorkflowEffects"]
