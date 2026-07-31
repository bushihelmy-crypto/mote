"""Unary model-attempt bridge from the logical gateway to InferenceRuntime."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from mote.contracts.inference.attempt import InferenceAttemptRequest
from mote.contracts.inference.events import AttemptEventType, AttemptLifecycleEvent
from mote.contracts.inference.wire_permit import ExecutionTaxonomy
from mote.contracts.model.invocation import CanonicalModelResponse
from mote.contracts.model.model_journal import ModelWireAuthorizedRecord
from mote.contracts.ports.inference.inference_runtime import InferenceRuntime
from mote.contracts.ports.inference.wire_permit import WirePermitIssuer


class RuntimeAttemptFailure(RuntimeError):
    def __init__(self, terminal: AttemptLifecycleEvent) -> None:
        self.terminal = terminal
        super().__init__(str(terminal.payload.get("reason", terminal.event_type.value)))


@dataclass(frozen=True)
class RuntimeAttemptResult:
    response: CanonicalModelResponse
    stream_chunks: tuple[dict[str, object], ...]


AuthorizationAppender = Callable[[ModelWireAuthorizedRecord], Awaitable[None]]


class InferenceAttemptExecutor:
    def __init__(
        self,
        runtime: InferenceRuntime,
        permit_issuer: WirePermitIssuer,
        *,
        permit_audience: str,
        epoch_provider: Callable[[], tuple[int, int]],
        permit_lifetime_seconds: float = 30.0,
    ) -> None:
        if not permit_audience or permit_lifetime_seconds <= 0:
            raise ValueError("model attempt permit configuration is invalid")
        self._runtime = runtime
        self._permit_issuer = permit_issuer
        self._permit_audience = permit_audience
        self._epoch_provider = epoch_provider
        self._permit_lifetime_seconds = permit_lifetime_seconds

    async def execute(
        self,
        request: InferenceAttemptRequest,
        *,
        ordinal: int,
        resume_generation: int,
        issued_journal_revision: int,
        append_authorization: AuthorizationAppender,
    ) -> RuntimeAttemptResult:
        execution = await self._runtime.start_attempt(request)
        chunks: list[dict[str, object]] = []
        authorization_seen = False
        async for event in execution:
            if event.event_type is AttemptEventType.WIRE_AUTHORIZATION_REQUIRED:
                if authorization_seen:
                    raise RuntimeError("runtime requested wire authorization twice")
                authorization_seen = True
                now = datetime.now(timezone.utc)
                backup_epoch, admission_epoch = self._epoch_provider()
                permit = self._permit_issuer.issue(
                    attempt_id=request.attempt_id,
                    execution_taxonomy=ExecutionTaxonomy.UNARY_FINITE_ATTEMPT,
                    owner_journal_id=request.owner_journal_id,
                    wire_unit=str(request.invocation.get("operation", "generate")),
                    generation_id=request.generation_id,
                    generation_artifact_digest=request.generation_artifact_digest,
                    ordinal=ordinal,
                    issued_journal_revision=issued_journal_revision,
                    not_before=now,
                    expires_at=now + timedelta(seconds=self._permit_lifetime_seconds),
                    audience=self._permit_audience,
                    backup_epoch=backup_epoch,
                    admission_epoch=admission_epoch,
                )
                digest = "sha256:" + hashlib.sha256(permit.model_dump_json().encode()).hexdigest()
                await append_authorization(
                    ModelWireAuthorizedRecord(
                        model_call_id=request.model_call_id,
                        attempt_id=request.attempt_id,
                        ordinal=ordinal,
                        resume_generation=resume_generation,
                        issued_journal_revision=issued_journal_revision,
                        permit_digest=digest,
                    )
                )
                await execution.authorize_wire(permit)
                continue
            if event.event_type is AttemptEventType.STREAM_CHUNK:
                chunk = event.payload.get("chunk")
                if isinstance(chunk, dict):
                    chunks.append(chunk)
                continue
            if event.event_type is AttemptEventType.SUCCEEDED:
                if not authorization_seen:
                    raise RuntimeError("runtime succeeded without wire authorization")
                payload = event.payload.get("result")
                if not isinstance(payload, dict):
                    raise RuntimeError("runtime success has no canonical result")
                return RuntimeAttemptResult(
                    response=CanonicalModelResponse.model_validate(payload),
                    stream_chunks=tuple(chunks),
                )
            if event.terminal:
                raise RuntimeAttemptFailure(event)
        raise RuntimeError("runtime attempt ended without a terminal event")


__all__ = [
    "InferenceAttemptExecutor",
    "RuntimeAttemptFailure",
    "RuntimeAttemptResult",
]
