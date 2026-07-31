"""Product owners for one durable command or artifact-transfer wire unit."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone

from mote.contracts.inference.events import AttemptEventType
from mote.contracts.inference.executions import BoundExecutionRequest, TransferPartRequest
from mote.contracts.inference.wire_permit import ExecutionTaxonomy
from mote.contracts.ports.artifact.provider_transfer import ProviderArtifactTransferRuntime
from mote.contracts.ports.inference.wire_permit import WirePermitIssuer
from mote.contracts.ports.service.command_runtime import ServiceCommandRuntime

EpochProvider = Callable[[], tuple[int, int]]
CommandRequestFactory = Callable[[str, Mapping[str, object]], BoundExecutionRequest]
TransferRequestFactory = Callable[[str, Mapping[str, object]], TransferPartRequest]


class RuntimeCommandGateway:
    def __init__(
        self,
        runtime: ServiceCommandRuntime,
        permit_issuer: WirePermitIssuer,
        request_factory: CommandRequestFactory,
        *,
        permit_audience: str,
        epoch_provider: EpochProvider,
        permit_lifetime_seconds: float = 30.0,
    ) -> None:
        if not permit_audience or permit_lifetime_seconds <= 0:
            raise ValueError("command permit configuration is invalid")
        self._runtime = runtime
        self._permit_issuer = permit_issuer
        self._request_factory = request_factory
        self._permit_audience = permit_audience
        self._epoch_provider = epoch_provider
        self._permit_lifetime_seconds = permit_lifetime_seconds

    async def execute(self, operation: str, payload: Mapping[str, object]) -> dict[str, object]:
        request = self._request_factory(operation, payload)
        execution = await self._runtime.start_command(request)
        authorization_seen = False
        async for event in execution:
            if event.event_type is AttemptEventType.WIRE_AUTHORIZATION_REQUIRED:
                if authorization_seen:
                    raise RuntimeError("runtime requested command authorization twice")
                authorization_seen = True
                await execution.authorize_wire(
                    _permit(
                        self._permit_issuer,
                        request,
                        taxonomy=ExecutionTaxonomy.DURABLE_OPERATION,
                        audience=self._permit_audience,
                        epoch_provider=self._epoch_provider,
                        lifetime_seconds=self._permit_lifetime_seconds,
                        revision=event.receipt_revision,
                    )
                )
            elif event.event_type is AttemptEventType.SUCCEEDED:
                if not authorization_seen:
                    raise RuntimeError("command succeeded without wire authorization")
                result = event.payload.get("result")
                if not isinstance(result, dict):
                    raise RuntimeError("command terminal event omitted result")
                return result
            elif event.terminal:
                raise RuntimeError(str(event.payload.get("reason", event.event_type.value)))
        raise RuntimeError("command runtime ended without terminal event")


class RuntimeArtifactTransferGateway:
    def __init__(
        self,
        runtime: ProviderArtifactTransferRuntime,
        permit_issuer: WirePermitIssuer,
        request_factory: TransferRequestFactory,
        *,
        permit_audience: str,
        epoch_provider: EpochProvider,
        permit_lifetime_seconds: float = 30.0,
    ) -> None:
        if not permit_audience or permit_lifetime_seconds <= 0:
            raise ValueError("transfer permit configuration is invalid")
        self._runtime = runtime
        self._permit_issuer = permit_issuer
        self._request_factory = request_factory
        self._permit_audience = permit_audience
        self._epoch_provider = epoch_provider
        self._permit_lifetime_seconds = permit_lifetime_seconds

    async def execute_part(self, operation: str, payload: Mapping[str, object]) -> dict[str, object]:
        request = self._request_factory(operation, payload)
        execution = await self._runtime.execute_part(request)
        authorization_seen = False
        async for event in execution:
            if event.event_type is AttemptEventType.WIRE_AUTHORIZATION_REQUIRED:
                if authorization_seen:
                    raise RuntimeError("runtime requested transfer authorization twice")
                authorization_seen = True
                await execution.authorize_wire(
                    _permit(
                        self._permit_issuer,
                        request,
                        taxonomy=ExecutionTaxonomy.ARTIFACT_TRANSFER,
                        audience=self._permit_audience,
                        epoch_provider=self._epoch_provider,
                        lifetime_seconds=self._permit_lifetime_seconds,
                        revision=event.receipt_revision,
                    )
                )
            elif event.event_type is AttemptEventType.SUCCEEDED:
                if not authorization_seen:
                    raise RuntimeError("transfer succeeded without wire authorization")
                result = event.payload.get("result")
                if not isinstance(result, dict):
                    raise RuntimeError("transfer terminal event omitted result")
                return result
            elif event.terminal:
                raise RuntimeError(str(event.payload.get("reason", event.event_type.value)))
        raise RuntimeError("transfer runtime ended without terminal event")


def _permit(
    issuer: WirePermitIssuer,
    request: BoundExecutionRequest,
    *,
    taxonomy: ExecutionTaxonomy,
    audience: str,
    epoch_provider: EpochProvider,
    lifetime_seconds: float,
    revision: int,
):
    now = datetime.now(timezone.utc)
    backup_epoch, admission_epoch = epoch_provider()
    return issuer.issue(
        attempt_id=request.execution_id,
        execution_taxonomy=taxonomy,
        owner_journal_id=request.owner_journal_id,
        wire_unit=request.operation,
        generation_id=request.generation_id,
        generation_artifact_digest=request.generation_artifact_digest,
        ordinal=1,
        issued_journal_revision=revision,
        not_before=now,
        expires_at=now + timedelta(seconds=lifetime_seconds),
        audience=audience,
        backup_epoch=backup_epoch,
        admission_epoch=admission_epoch,
    )


__all__ = ["RuntimeArtifactTransferGateway", "RuntimeCommandGateway"]
