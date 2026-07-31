"""Model inference operation for the ReAct execution graph."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from mote.contracts.conversation import UserMessage
from mote.contracts.execution.models import InferenceCheckpointState, MutationStatus
from mote.contracts.model.inference import InferenceAttemptFence
from mote.contracts.ports.execution.checkpoint import InferenceCheckpointPort
from mote.contracts.ports.execution.transaction import ExecutionTransactionPort
from mote.kernel.commands.contracts import HistoryProjection
from mote.kernel.telemetry.events import span


class InferenceService:
    """Prepare and start one model request behind a recoverable checkpoint."""

    def __init__(
        self,
        *,
        is_active: Callable[[], bool],
        checkpoint: InferenceCheckpointPort,
        context_provider: Any,
        inference_engine: Any,
        output_engine: Any,
        transaction: ExecutionTransactionPort,
        set_channel: Callable[[Any], None],
        set_tool_snapshot: Callable[[Any], None],
        turn_context_bus: Any = None,
        get_cwd: Callable[[], str] | None = None,
    ) -> None:
        self._is_active = is_active
        self._checkpoint = checkpoint
        self._context_provider = context_provider
        self._inference_engine = inference_engine
        self._output_engine = output_engine
        self._transaction = transaction
        self._set_channel = set_channel
        self._set_tool_snapshot = set_tool_snapshot
        self._turn_context_bus = turn_context_bus
        self._get_cwd = get_cwd

    async def infer(self) -> bool:
        if not self._is_active():
            return False
        if self._checkpoint.reinstate():
            return True
        async with span("inference"):
            resumed = self._checkpoint.resume()
            model_call_id = resumed.model_call_id if resumed is not None else uuid4().hex
            await self._record_turn_context(model_call_id)
            request = await self._context_provider.prepare()
            target = await self._context_provider.resolve_inference_target(
                request,
                model_call_id=model_call_id,
            )
            transferred = False
            try:
                request = self._context_provider.finalize_for_model(request, target)
                self._set_channel(request.command_channel)
                self._set_tool_snapshot(request.tool_snapshot)
                resume = resumed is not None
                snapshot = request.tool_snapshot
                attempt = InferenceAttemptFence(
                    model_call_id,
                    uuid4().hex,
                    (resumed.inference_fencing_token + 1) if resumed is not None else 1,
                )
                checkpoint_state = InferenceCheckpointState(
                    model_call_id=model_call_id,
                    target_id=target.lease.target_id,
                    route_schema_version=2,
                    target_lease_id=target.lease.lease_id,
                    target_lease_expires_at=target.lease.expires_at,
                    inference_attempt_id=attempt.attempt_id,
                    inference_fencing_token=attempt.fencing_token,
                    capability_fingerprint=target.capability_fingerprint,
                    projection_compatibility_key=target.projection_compatibility_key,
                    tool_snapshot_id=snapshot.snapshot_id if snapshot is not None else "",
                    tool_registry_revision=(snapshot.registry_revision if snapshot is not None else 0),
                    tool_projection_fingerprint=request.tool_projection_fingerprint,
                    protocol_fingerprint=request.protocol_fingerprint,
                    vocabulary_fingerprint=request.vocabulary_fingerprint,
                    prompt_section_set_fingerprint=request.prompt_section_set_fingerprint,
                    request_fingerprint=self._request_fingerprint(request),
                )
                if not resume:
                    self._checkpoint.begin_call(checkpoint_state)
                else:
                    self._checkpoint.refresh(checkpoint_state)
                await self._inference_engine.start(
                    request.req,
                    request.system_prompt,
                    tool_specs=request.tool_specs,
                    target=target,
                    model_call_id=model_call_id,
                    resume=resume,
                    output_binding=request.output_binding.binding,
                    output_schema=request.output_schema,
                    output_run_id=self._output_engine.run_id,
                    schema_fingerprint=request.schema_fingerprint,
                    attempt=attempt,
                    protocol_fingerprint=request.protocol_fingerprint,
                    vocabulary_fingerprint=request.vocabulary_fingerprint,
                    tool_projection_fingerprint=request.tool_projection_fingerprint,
                    prompt_section_set_fingerprint=request.prompt_section_set_fingerprint,
                    request_fingerprint=checkpoint_state.request_fingerprint,
                )
                transferred = True
            finally:
                if not transferred:
                    await self._context_provider.release_inference_target(target)
        return True

    @staticmethod
    def _request_fingerprint(request) -> str:
        payload = {
            "messages": [
                message.model_dump(mode="json") if hasattr(message, "model_dump") else message
                for message in request.req
            ],
            "system_prompt": request.system_prompt,
            "tools": request.tool_specs,
            "schema_fingerprint": request.schema_fingerprint,
            "tool_projection_fingerprint": request.tool_projection_fingerprint,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()

    async def _record_turn_context(self, model_call_id: str) -> None:
        if self._turn_context_bus is None:
            return
        cwd = self._get_cwd() if self._get_cwd is not None else None
        block = await self._turn_context_bus.collect_to_context(cwd=cwd or None)
        if block:
            message = UserMessage(content=block)
            result = await self._transaction.record_history(
                self._transaction.context(f"{model_call_id}:turn-context"),
                HistoryProjection((message,), message.id),
            )
            if result.status not in {
                MutationStatus.APPLIED,
                MutationStatus.ALREADY_APPLIED,
            }:
                raise RuntimeError(result.reason or result.status.value)


__all__ = ["InferenceService"]
