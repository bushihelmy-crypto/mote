"""ModelCall-backed inference checkpoint and Session projection coordinator."""

from __future__ import annotations

from dataclasses import replace

from mote.contracts.artifact import ArtifactResolutionPolicy, ArtifactSensitivity
from mote.contracts.execution.models import InferenceCheckpointAttemptState, InferenceCheckpointState
from mote.contracts.model.failover import ModelCallState
from mote.contracts.model.inference import InferenceResult
from mote.contracts.model.invocation import GenerateOutput
from mote.contracts.ports.artifact.store import ArtifactResolver
from mote.contracts.ports.model.recovery import ModelCallRecoveryQuery, ModelRecoveryDisposition
from mote.kernel.inference.base import BaseInferenceEngine
from mote.runtime.models.session_projection import (
    ModelSessionProjectionRecord,
    ModelSessionProjectionState,
    ModelSessionProjectionStore,
)


class InferenceCheckpoint:
    """Keep one ModelCall identity until its Session projection is acknowledged."""

    def __init__(
        self,
        *,
        projections: ModelSessionProjectionStore,
        model_calls: ModelCallRecoveryQuery,
        inference_engine: BaseInferenceEngine,
        artifact_resolver: ArtifactResolver,
    ) -> None:
        self._projections = projections
        self._model_calls = model_calls
        self._inference_engine = inference_engine
        self._artifact_resolver = artifact_resolver
        self._model_call_id: str | None = None
        self._reinstated = False
        self._state: InferenceCheckpointState | None = None

    async def reinstate(self) -> bool:
        pending = self._pending()
        if pending is None:
            return False
        if pending.state is ModelSessionProjectionState.CALL_STARTED:
            output = await self._terminal_output(pending.model_call_id)
            if output is None:
                return False
            self._projections.commit_intent(pending.model_call_id, output)
        else:
            output = pending.output
            if output is None:
                raise RuntimeError("Model Session projection intent omitted its output")
        self._adopt(pending, output)
        return True

    @property
    def step_id(self) -> str | None:
        return self._model_call_id

    @property
    def reinstated(self) -> bool:
        return self._reinstated

    def begin_call(self, state: InferenceCheckpointState) -> None:
        if self._model_call_id is not None:
            raise RuntimeError("a ModelCall checkpoint is already active")
        self._projections.begin(state)
        self._model_call_id = state.model_call_id
        self._state = state

    def resume(self) -> InferenceCheckpointState | None:
        pending = self._pending()
        if pending is None:
            return None
        inspection = self._model_calls.inspect_recovery(pending.model_call_id)
        recovery = inspection.recovery
        if inspection.disposition is ModelRecoveryDisposition.ABSENT:
            if pending.checkpoint.attempt_state is not InferenceCheckpointAttemptState.INTENT_COMMITTED:
                self._projections.require_owner_action(pending.model_call_id)
                raise RuntimeError("ModelCall ABSENT evidence is only recoverable before wire start")
            self._model_call_id = pending.model_call_id
            self._state = pending.checkpoint
            return pending.checkpoint
        if inspection.disposition is ModelRecoveryDisposition.RECOVERABLE:
            self._model_call_id = pending.model_call_id
            self._state = pending.checkpoint
            return pending.checkpoint
        if recovery is not None and recovery.state is ModelCallState.SUCCEEDED:
            # infer() calls reinstate before resume; reaching this branch means a
            # corrupt/unsupported terminal response failed to project.
            raise RuntimeError("successful ModelCall was not reinstated into its Session projection")
        if inspection.disposition is ModelRecoveryDisposition.TERMINAL:
            self._projections.require_owner_action(pending.model_call_id)
            state = recovery.state.value if recovery is not None else "unknown"
            raise RuntimeError(f"ModelCall is terminal without a projectable result: {state}")
        self._projections.require_owner_action(pending.model_call_id)
        raise RuntimeError(f"ModelCall recovery failed closed: {inspection.disposition.value}")

    def refresh(self, state: InferenceCheckpointState) -> None:
        if self._model_call_id != state.model_call_id:
            raise RuntimeError("ModelCall checkpoint refresh identity mismatch")
        self._projections.refresh(state)
        self._state = state

    def mark_wire_started(self) -> None:
        state = self._state
        if state is None or state.attempt_state is not InferenceCheckpointAttemptState.INTENT_COMMITTED:
            raise RuntimeError("ModelCall wire start requires a committed intent")
        started = replace(state, attempt_state=InferenceCheckpointAttemptState.WIRE_STARTED)
        self._projections.refresh(started)
        self._state = started

    async def record_result(self) -> None:
        model_call_id = self._model_call_id
        if model_call_id is None or self._reinstated:
            return
        current = self._projections.get(model_call_id)
        if current is not None and current.state is ModelSessionProjectionState.INTENT_COMMITTED:
            return
        if self._state is not None and self._state.attempt_state is InferenceCheckpointAttemptState.WIRE_STARTED:
            settled = replace(self._state, attempt_state=InferenceCheckpointAttemptState.SETTLED)
            self._projections.refresh(settled)
            self._state = settled
        output = await self._terminal_output(model_call_id)
        if output is None:
            self._projections.require_owner_action(model_call_id)
            raise RuntimeError("ModelCall result is not durably committed or cannot project to Session")
        self._projections.commit_intent(model_call_id, output)

    def discard(self) -> None:
        model_call_id = self._take()
        if model_call_id is None:
            return
        record = self._projections.get(model_call_id)
        if record is not None and record.state is ModelSessionProjectionState.INTENT_COMMITTED:
            self._projections.acknowledge(model_call_id)

    def abort(self) -> None:
        model_call_id = self._model_call_id
        if model_call_id is not None:
            record = self._projections.get(model_call_id)
            if self._state is not None and self._state.attempt_state is InferenceCheckpointAttemptState.WIRE_STARTED:
                in_doubt = replace(self._state, attempt_state=InferenceCheckpointAttemptState.IN_DOUBT)
                self._projections.refresh(in_doubt)
                self._state = in_doubt
            if record is not None and record.state in {
                ModelSessionProjectionState.CALL_STARTED,
                ModelSessionProjectionState.INTENT_COMMITTED,
            }:
                self._projections.require_owner_action(model_call_id)
        self._take()

    def _pending(self) -> ModelSessionProjectionRecord | None:
        pending = self._projections.pending()
        return pending[-1] if pending else None

    async def _terminal_output(self, model_call_id: str) -> GenerateOutput | None:
        inspection = self._model_calls.inspect_recovery(model_call_id)
        recovery = inspection.recovery
        if (
            inspection.disposition is not ModelRecoveryDisposition.TERMINAL
            or recovery is None
            or recovery.state is not ModelCallState.SUCCEEDED
            or recovery.terminal is None
        ):
            return None
        response = recovery.terminal.accepted_response
        output = response.output if response is not None and isinstance(response.output, GenerateOutput) else None
        if output is None or output.content_artifact is None:
            return output
        resolved = await self._artifact_resolver.resolve(
            output.content_artifact,
            ArtifactResolutionPolicy(
                max_bytes=output.content_artifact.size,
                allowed_sensitivities=frozenset({ArtifactSensitivity.PRIVATE}),
            ),
        )
        try:
            content = resolved.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError("Model response Artifact is not UTF-8 text") from exc
        return output.model_copy(update={"content": content, "content_artifact": None})

    def _adopt(self, record: ModelSessionProjectionRecord, output: GenerateOutput) -> None:
        self._model_call_id = record.model_call_id
        self._state = record.checkpoint
        self._reinstated = True
        self._inference_engine.reinstate(
            InferenceResult(
                content=output.content,
                tool_calls=output.tool_calls or None,
                structured_value=output.structured,
            )
        )

    def _take(self) -> str | None:
        model_call_id, self._model_call_id = self._model_call_id, None
        self._reinstated = False
        self._state = None
        return model_call_id


__all__ = ["InferenceCheckpoint"]
