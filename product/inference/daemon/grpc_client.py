"""Retry-free Shared Process gRPC client and receipt-based event resumption."""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any

import grpc
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mote.contracts.inference.shared import ProtocolNegotiationResult, SharedHandshake, SharedSessionCredential
from mote.product.inference.daemon.rpc import gateway_v1_pb2 as _pb
from mote.product.inference.daemon.rpc import gateway_v1_pb2_grpc as rpc
from mote.product.inference.security.permit_issuer import ProductWirePermitIssuer
from mote.product.inference.security.wire_permit import Ed25519WirePermitSigner

pb: Any = _pb


class SharedGrpcClient:
    """One authenticated connection to a generation-suffixed daemon socket."""

    def __init__(
        self,
        socket_path: Path,
        *,
        max_receive_bytes: int = 16 * 1024 * 1024,
        max_send_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        if not socket_path.is_absolute():
            raise ValueError("Shared daemon socket path must be absolute")
        credentials = grpc.local_channel_credentials(grpc.LocalConnectionType.UDS)
        self._channel = grpc.aio.secure_channel(
            f"unix:{socket_path}",
            credentials,
            options=(
                ("grpc.max_receive_message_length", max_receive_bytes),
                ("grpc.max_send_message_length", max_send_bytes),
                ("grpc.enable_retries", 0),
            ),
        )
        self._execution = rpc.GatewayExecutionServiceStub(self._channel)
        self._control = rpc.GatewayControlServiceStub(self._channel)
        self._credential: SharedSessionCredential | None = None

    async def authenticate(
        self,
        handshake: SharedHandshake,
        *,
        capabilities: Iterable[str] = (),
    ) -> ProtocolNegotiationResult:
        negotiation = await self._control.Negotiate(
            pb.NegotiationRequest(
                protocol_versions=handshake.protocol_versions,
                capabilities=tuple(capabilities),
            )
        )
        response = await self._control.Authenticate(
            pb.HandshakeRequest(signed_handshake=handshake.model_dump_json().encode())
        )
        credential = SharedSessionCredential.model_validate_json(response.session_credential)
        if (
            response.protocol_version != negotiation.protocol_version
            or credential.protocol_version != negotiation.protocol_version
            or response.daemon_socket_generation != negotiation.daemon_socket_generation
            or credential.socket_generation != negotiation.daemon_socket_generation
        ):
            raise RuntimeError("Shared negotiation and authentication disagree")
        self._credential = credential
        return ProtocolNegotiationResult(
            protocol_version=negotiation.protocol_version,
            capabilities=tuple(negotiation.capabilities),
            socket_generation=negotiation.daemon_socket_generation,
        )

    async def negotiate(
        self,
        protocol_versions: Iterable[int],
        *,
        capabilities: Iterable[str] = (),
    ) -> ProtocolNegotiationResult:
        response = await self._control.Negotiate(
            pb.NegotiationRequest(
                protocol_versions=tuple(protocol_versions),
                capabilities=tuple(capabilities),
            )
        )
        return ProtocolNegotiationResult(
            protocol_version=response.protocol_version,
            capabilities=tuple(response.capabilities),
            socket_generation=response.daemon_socket_generation,
        )

    def envelope(self, **fields: Any) -> Any:
        credential = self._require_credential()
        return pb.Envelope(
            schema_version=1,
            protocol_version=credential.protocol_version,
            principal_proof=credential.model_dump_json(),
            **fields,
        )

    def permit_issuer(self) -> ProductWirePermitIssuer:
        credential = self._require_credential()
        padding = "=" * (-len(credential.permit_private_key) % 4)
        private_bytes = base64.b64decode(
            credential.permit_private_key + padding,
            altchars=b"-_",
            validate=True,
        )
        private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
        signer = Ed25519WirePermitSigner(
            issuer_key_id=credential.permit_issuer_key_id,
            trust_revision=credential.permit_trust_revision,
            private_key=private_key,
        )
        return ProductWirePermitIssuer(
            signer,
            issuer_key_id=credential.permit_issuer_key_id,
            trust_revision=credential.permit_trust_revision,
        )

    async def start_unary(self, request: Any, *, timeout: float | None = None) -> Any:
        return await self._execution.StartUnary(request, timeout=timeout)

    async def start_durable_command(self, request: Any, *, timeout: float | None = None) -> Any:
        return await self._execution.StartDurableCommand(request, timeout=timeout)

    async def open_session(self, request: Any, *, timeout: float | None = None) -> Any:
        return await self._execution.OpenSession(request, timeout=timeout)

    async def execute_transfer_part(self, request: Any, *, timeout: float | None = None) -> Any:
        return await self._execution.ExecuteTransferPart(request, timeout=timeout)

    async def authorize_wire(
        self,
        execution_id: str,
        wire_permit: bytes,
        *,
        generation_id: str,
        timeout: float | None = None,
    ) -> None:
        await self._execution.AuthorizeWire(
            pb.AuthorizeRequest(
                envelope=self.envelope(
                    generation_id=generation_id,
                    idempotency_key=f"authorize:{execution_id}",
                ),
                execution_id=execution_id,
                wire_permit=wire_permit,
            ),
            timeout=timeout,
        )

    async def cancel(
        self,
        execution_id: str,
        reason: str,
        *,
        generation_id: str,
        timeout: float | None = None,
    ) -> None:
        if not reason:
            raise ValueError("cancellation reason is required")
        await self._execution.Cancel(
            pb.CancelRequest(
                envelope=self.envelope(
                    generation_id=generation_id,
                    idempotency_key=f"cancel:{execution_id}",
                ),
                execution_id=execution_id,
                reason=reason,
            ),
            timeout=timeout,
        )

    def session(self, requests: AsyncIterator[Any]) -> AsyncIterator[Any]:
        return self._execution.Session(requests)

    async def stage_generation(self, artifact: bytes, *, generation_id: str, artifact_digest: str) -> Any:
        return await self._control.StageGeneration(
            pb.GenerationRequest(
                envelope=self.envelope(
                    generation_id=generation_id,
                    generation_artifact_digest=artifact_digest,
                    idempotency_key=f"stage:{generation_id}:{artifact_digest}",
                ),
                generation_artifact=artifact,
            )
        )

    async def observe_generation(self, generation_id: str, *, artifact_digest: str = "") -> Any:
        return await self._control.ObserveGeneration(
            pb.GenerationRequest(
                envelope=self.envelope(
                    generation_id=generation_id,
                    generation_artifact_digest=artifact_digest,
                    idempotency_key=f"observe:{generation_id}",
                )
            )
        )

    async def get_readiness(self, *, timeout: float | None = None) -> Any:
        return await self._control.GetReadiness(pb.Empty(), timeout=timeout)

    async def backup(self, destination: Path, *, consistency: str, timeout: float | None = None) -> Any:
        return await self._control.Backup(
            pb.BackupRequest(
                envelope=self.envelope(idempotency_key=f"backup:{destination}"),
                destination=str(destination),
                consistency=consistency,
            ),
            timeout=timeout,
        )

    async def verify_restore(self, source: Path, *, timeout: float | None = None) -> Any:
        return await self._control.VerifyRestore(
            pb.VerifyRestoreRequest(
                envelope=self.envelope(idempotency_key=f"verify-restore:{source}"),
                source=str(source),
            ),
            timeout=timeout,
        )

    async def reconcile_all(self, *, timeout: float | None = None) -> Any:
        return await self._control.ReconcileAll(
            pb.ReconcileAllRequest(envelope=self.envelope(idempotency_key="reconcile-all")),
            timeout=timeout,
        )

    async def begin_drain(self, *, timeout_seconds: float, timeout: float | None = None) -> Any:
        return await self._control.BeginDrain(
            pb.DrainRequest(
                envelope=self.envelope(idempotency_key="begin-drain"),
                timeout_seconds=timeout_seconds,
            ),
            timeout=timeout,
        )

    async def query_receipt(
        self,
        execution_id: str,
        *,
        generation_id: str,
        timeout: float | None = None,
    ) -> Any:
        return await self._execution.QueryReceipt(
            pb.ReceiptRequest(
                envelope=self.envelope(
                    generation_id=generation_id,
                    idempotency_key=f"query:{execution_id}",
                ),
                execution_id=execution_id,
            ),
            timeout=timeout,
        )

    async def resume_events(
        self,
        execution_id: str,
        *,
        generation_id: str,
        after_sequence: int,
        receipt_revision: int,
        timeout: float | None = None,
    ) -> AsyncIterator[Any]:
        """Query durable truth, then resume and suppress at-least-once duplicates."""
        receipt = await self.query_receipt(execution_id, generation_id=generation_id, timeout=timeout)
        revision = max(receipt_revision, receipt.revision)
        cursor = after_sequence
        request = pb.CursorRequest(
            envelope=self.envelope(
                generation_id=generation_id,
                idempotency_key=f"events:{execution_id}:{cursor}",
            ),
            execution_id=execution_id,
            after_sequence=cursor,
            receipt_revision=revision,
        )
        pending: dict[int, Any] = {}
        async for event in self._execution.StreamEvents(request, timeout=timeout):
            if event.execution_id != execution_id or event.sequence <= cursor:
                continue
            pending[event.sequence] = event
            if len(pending) > 256:
                raise RuntimeError("Shared event reorder window exceeded")
            while cursor + 1 in pending:
                cursor += 1
                yield pending.pop(cursor)
        if pending:
            raise RuntimeError("Shared event stream ended with a sequence gap")

    async def close(self) -> None:
        await self._channel.close()

    def _require_credential(self) -> SharedSessionCredential:
        if self._credential is None:
            raise RuntimeError("Shared gRPC client is not authenticated")
        return self._credential
