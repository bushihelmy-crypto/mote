"""Generation-pinned construction of public inference compatibility owners."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from uuid import uuid4

from aiohttp import web

from mote.contracts.artifact import ArtifactRef
from mote.contracts.inference.deadline import CrossProcessDeadline
from mote.contracts.inference.epochs import ExecutionEpochSource
from mote.contracts.inference.executions import BoundExecutionRequest, TransferPartRequest
from mote.contracts.inference.identity import InferencePrincipal, TrustedSchedulingClass
from mote.contracts.model.failover import EndpointDescriptor
from mote.contracts.ports.artifact.provider_transfer import ProviderArtifactTransferRuntime
from mote.contracts.ports.artifact.store import ArtifactLookupIndex, GenerationArtifactReader
from mote.contracts.ports.inference.session_runtime import SessionRuntime
from mote.contracts.ports.inference.wire_permit import WirePermitIssuer
from mote.contracts.ports.model.gateway import ModelGateway
from mote.contracts.ports.service.command_runtime import ServiceCommandRuntime
from mote.contracts.runtime.application import DefaultModelView
from mote.product.inference.command_gateway import RuntimeArtifactTransferGateway, RuntimeCommandGateway
from mote.product.inference.session_gateway import RuntimeSessionGateway
from mote.product.interfaces.inference_api.application import InferenceApiAuthorizer, build_inference_api
from mote.product.interfaces.inference_api.runtime_operations import (
    ArtifactTransferCompatibilityOwner,
    CommandCompatibilityOwner,
    ResponseCompatibilityOwner,
)


class InferenceRuntimeLease(Protocol):
    gateway: ModelGateway
    command_runtime: ServiceCommandRuntime | None
    session_runtime: SessionRuntime | None
    transfer_runtime: ProviderArtifactTransferRuntime | None
    permit_issuer: WirePermitIssuer | None
    epoch_source: ExecutionEpochSource | None
    permit_audience: str
    generation_id: str
    generation_artifact_digest: str
    default_model: DefaultModelView
    artifact_store: ArtifactLookupIndex | None
    artifact_reader: GenerationArtifactReader | None


@dataclass(frozen=True, slots=True)
class _RequestContext:
    generation_id: str
    artifact_digest: str
    endpoint: EndpointDescriptor
    credential_slot_id: str
    credential_version: str
    principal: InferencePrincipal
    deadline_seconds: float

    def request(self, operation: str, payload: Mapping[str, object]) -> BoundExecutionRequest:
        identifier = uuid4().hex
        now = datetime.now(timezone.utc)
        return BoundExecutionRequest(
            execution_id=f"operation-{identifier}",
            owner_journal_id=f"operation-journal-{identifier}",
            generation_id=self.generation_id,
            generation_artifact_digest=self.artifact_digest,
            endpoint_binding_id=self.endpoint.endpoint_id,
            credential_slot_id=self.credential_slot_id,
            credential_version=self.credential_version,
            operation=operation,
            payload=dict(payload),
            deadline=CrossProcessDeadline(
                deadline_utc=now + timedelta(seconds=self.deadline_seconds),
                remaining_seconds_at_send=self.deadline_seconds,
                sent_at_utc=now,
            ),
            principal=self.principal,
            scheduling=TrustedSchedulingClass(),
        )

    def transfer(self, operation: str, payload: Mapping[str, object]) -> TransferPartRequest:
        base = self.request(operation, payload)
        artifact = payload.get("artifact")
        if not isinstance(artifact, dict):
            raise ValueError("artifact transfer requires an artifact reference")
        try:
            ref = ArtifactRef(**artifact)
        except (TypeError, ValueError) as exc:
            raise ValueError("artifact transfer reference is invalid") from exc
        return TransferPartRequest(
            **base.model_dump(),
            transfer_id=f"transfer-{base.execution_id}",
            part_number=1,
            offset=0,
            length=ref.size or 1,
            content_digest=f"sha256:{ref.digest}",
        )


def build_generation_inference_api(
    lease: InferenceRuntimeLease,
    *,
    endpoint: EndpointDescriptor,
    credential_slot_id: str,
    credential_version: str,
    bearer_token: str | None = None,
    authorizer: InferenceApiAuthorizer | None = None,
    artifact_store: ArtifactLookupIndex | None = None,
    artifact_reader: GenerationArtifactReader | None = None,
    deadline_seconds: float = 60.0,
) -> web.Application:
    if deadline_seconds <= 0:
        raise ValueError("inference operation deadline must be positive")
    if not lease.generation_id or not lease.generation_artifact_digest:
        raise ValueError("runtime lease has no generation identity")
    if artifact_store is None:
        artifact_store = lease.artifact_store
    if artifact_reader is None:
        artifact_reader = lease.artifact_reader
    issuer = lease.permit_issuer
    epoch_source = lease.epoch_source
    audience = lease.permit_audience
    principal = InferencePrincipal(
        tenant_id="mote-application",
        project_id="inference-api",
        subject_id="compatibility-api",
        policy_revision=lease.generation_id,
        delegation_digest="sha256:" + hashlib.sha256(lease.generation_id.encode()).hexdigest(),
    )
    context = _RequestContext(
        generation_id=lease.generation_id,
        artifact_digest=lease.generation_artifact_digest,
        endpoint=endpoint,
        credential_slot_id=credential_slot_id,
        credential_version=credential_version,
        principal=principal,
        deadline_seconds=deadline_seconds,
    )
    durable = None
    responses = None
    realtime = None
    artifacts = None
    if lease.command_runtime is not None:
        if issuer is None or epoch_source is None or not audience:
            raise ValueError("command runtime requires generation permit authority")
        durable = CommandCompatibilityOwner(
            RuntimeCommandGateway(
                lease.command_runtime,
                issuer,
                context.request,
                permit_audience=audience,
                epoch_provider=lambda: epoch_source.snapshot().pair(),
            )
        )
        responses = ResponseCompatibilityOwner(
            RuntimeCommandGateway(
                lease.command_runtime,
                issuer,
                context.request,
                permit_audience=audience,
                epoch_provider=lambda: epoch_source.snapshot().pair(),
            )
        )
    if lease.session_runtime is not None:
        if issuer is None or epoch_source is None or not audience:
            raise ValueError("session runtime requires generation permit authority")
        realtime = RuntimeSessionGateway(
            lease.session_runtime,
            issuer,
            lambda payload: context.request("realtime.open", payload),
            permit_audience=audience,
            epoch_provider=lambda: epoch_source.snapshot().pair(),
        )
    if lease.transfer_runtime is not None:
        if issuer is None or epoch_source is None or not audience:
            raise ValueError("transfer runtime requires generation permit authority")
        artifacts = ArtifactTransferCompatibilityOwner(
            RuntimeArtifactTransferGateway(
                lease.transfer_runtime,
                issuer,
                context.transfer,
                permit_audience=audience,
                epoch_provider=lambda: epoch_source.snapshot().pair(),
            ),
            artifact_store,
        )
    return build_inference_api(
        lease.gateway,
        bearer_token=bearer_token,
        authorizer=authorizer,
        durable_operations=durable,
        durable_responses=responses,
        realtime_sessions=realtime,
        artifact_operations=artifacts,
        artifact_reader=artifact_reader,
    )


__all__ = ["InferenceRuntimeLease", "build_generation_inference_api"]
