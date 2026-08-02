"""UDS gRPC adapter for the Shared Process gateway."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

import grpc

from mote.contracts.inference.executions import SessionApplicationMessage
from mote.contracts.inference.shared import ProtocolNegotiation, SharedHandshake, SharedSessionCredential
from mote.contracts.inference.wire_permit import WirePermit
from mote.product.inference.daemon.messages import (
    AuthorizeExecutionCommand,
    CancelExecutionCommand,
    EventCursor,
    ExecutionQuery,
    ExecutionReceiptView,
    GenerationCommand,
    LifecycleEventView,
    RpcEnvelopeBinding,
    SessionMessageCommand,
    StartExecutionCommand,
    TransferExecutionCommand,
)
from mote.product.inference.daemon.rpc import gateway_v1_pb2 as _pb
from mote.product.inference.daemon.rpc import gateway_v1_pb2_grpc as rpc
from mote.product.inference.daemon.security import SharedAuthenticationError, SharedHandshakeAuthority

pb: Any = _pb


class SharedExecutionBackend(Protocol):
    async def start_unary(self, request: StartExecutionCommand, credential: SharedSessionCredential) -> int: ...

    async def start_durable_command(
        self, request: StartExecutionCommand, credential: SharedSessionCredential
    ) -> int: ...

    async def open_session(self, request: StartExecutionCommand, credential: SharedSessionCredential) -> int: ...

    async def execute_transfer_part(
        self, request: TransferExecutionCommand, credential: SharedSessionCredential
    ) -> int: ...

    async def authorize(self, request: AuthorizeExecutionCommand, credential: SharedSessionCredential) -> None: ...

    async def cancel(self, request: CancelExecutionCommand, credential: SharedSessionCredential) -> None: ...

    def stream_events(
        self, request: EventCursor, credential: SharedSessionCredential
    ) -> AsyncIterator[LifecycleEventView]: ...

    async def query_receipt(
        self, request: ExecutionQuery, credential: SharedSessionCredential
    ) -> ExecutionReceiptView: ...

    def reconcile(
        self, request: ExecutionQuery, credential: SharedSessionCredential
    ) -> AsyncIterator[LifecycleEventView]: ...

    def session(
        self,
        requests: AsyncIterator[SessionMessageCommand],
        credential: SharedSessionCredential,
    ) -> AsyncIterator[LifecycleEventView]: ...


class SharedGenerationControl(Protocol):
    async def stage_generation(
        self, request: GenerationCommand, credential: SharedSessionCredential
    ) -> tuple[str, str, str]: ...

    async def observe_generation(
        self, request: GenerationCommand, credential: SharedSessionCredential
    ) -> tuple[str, str, str]: ...


class SharedOperationsControl(Protocol):
    async def backup(self, destination: Path, consistency: str) -> str: ...

    async def verify_restore(self, source: Path) -> str: ...

    async def reconcile_all(self) -> tuple[int, int]: ...

    async def begin_drain(self, *, timeout_seconds: float) -> None: ...


class _ExecutionService(rpc.GatewayExecutionServiceServicer):
    def __init__(
        self,
        backend: SharedExecutionBackend,
        authority: SharedHandshakeAuthority,
        admission: Callable[[], tuple[bool, Mapping[str, str]]],
    ) -> None:
        self._backend = backend
        self._authority = authority
        self._admission = admission

    async def StartUnary(self, request, context):
        await self._require_admission(context)
        credential = await _credential(request.envelope, context, self._authority)
        revision = await _backend_call(context, self._backend.start_unary(_start(request), credential))
        return pb.StartResponse(execution_id=request.execution_id, receipt_revision=revision)

    async def StartDurableCommand(self, request, context):
        await self._require_admission(context)
        credential = await _credential(request.envelope, context, self._authority)
        revision = await _backend_call(context, self._backend.start_durable_command(_start(request), credential))
        return pb.StartResponse(execution_id=request.execution_id, receipt_revision=revision)

    async def OpenSession(self, request, context):
        await self._require_admission(context)
        credential = await _credential(request.envelope, context, self._authority)
        revision = await _backend_call(context, self._backend.open_session(_start(request), credential))
        return pb.StartResponse(execution_id=request.execution_id, receipt_revision=revision)

    async def ExecuteTransferPart(self, request, context):
        await self._require_admission(context)
        credential = await _credential(request.start.envelope, context, self._authority)
        revision = await _backend_call(
            context,
            self._backend.execute_transfer_part(
                TransferExecutionCommand(
                    start=_start(request.start),
                    part_number=request.part_number,
                    offset=request.offset,
                    length=request.length,
                    content_digest=request.content_digest,
                ),
                credential,
            ),
        )
        return pb.StartResponse(
            execution_id=request.start.execution_id,
            receipt_revision=revision,
        )

    async def AuthorizeWire(self, request, context):
        credential = await _credential(request.envelope, context, self._authority)
        await _backend_call(
            context,
            self._backend.authorize(
                AuthorizeExecutionCommand(
                    envelope=_envelope(request.envelope),
                    execution_id=request.execution_id,
                    permit=WirePermit.model_validate_json(request.wire_permit),
                ),
                credential,
            ),
        )
        return pb.Empty()

    async def Cancel(self, request, context):
        credential = await _credential(request.envelope, context, self._authority)
        await _backend_call(
            context,
            self._backend.cancel(
                CancelExecutionCommand(
                    envelope=_envelope(request.envelope),
                    execution_id=request.execution_id,
                    reason=request.reason,
                ),
                credential,
            ),
        )
        return pb.Empty()

    async def StreamEvents(self, request, context):
        credential = await _credential(request.envelope, context, self._authority)
        try:
            async for event in self._backend.stream_events(_cursor(request), credential):
                yield _event_pb(event)
        except (KeyError, PermissionError, ValueError, RuntimeError) as exc:
            await _abort_backend_error(context, exc)

    async def QueryReceipt(self, request, context):
        credential = await _credential(request.envelope, context, self._authority)
        result = await _backend_call(context, self._backend.query_receipt(_query(request), credential))
        return pb.Receipt(
            execution_id=result.execution_id,
            revision=result.revision,
            state=result.state,
            terminal_artifact_reference=result.terminal_artifact_reference,
        )

    async def Reconcile(self, request, context):
        credential = await _credential(request.envelope, context, self._authority)
        try:
            async for event in self._backend.reconcile(_query(request), credential):
                yield _event_pb(event)
        except (KeyError, PermissionError, ValueError, RuntimeError) as exc:
            await _abort_backend_error(context, exc)

    async def Session(self, request_iterator, context):
        first = await anext(request_iterator, None)
        if first is None:
            return
        credential = await _credential(first.envelope, context, self._authority)

        async def requests():
            yield _session_message(first)
            async for request in request_iterator:
                observed = await _credential(request.envelope, context, self._authority)
                if observed.session_id != credential.session_id:
                    await context.abort(
                        grpc.StatusCode.UNAUTHENTICATED,
                        "session credential changed within stream",
                    )
                yield _session_message(request)

        try:
            async for event in self._backend.session(requests(), credential):
                yield _event_pb(event)
        except (KeyError, PermissionError, ValueError, RuntimeError) as exc:
            await _abort_backend_error(context, exc)

    async def _require_admission(self, context) -> None:
        ready, _components = self._admission()
        if not ready:
            await context.abort(
                grpc.StatusCode.UNAVAILABLE,
                "Shared daemon admission is closed",
            )


class _ControlService(rpc.GatewayControlServiceServicer):
    def __init__(
        self,
        authority: SharedHandshakeAuthority,
        generations: SharedGenerationControl,
        *,
        readiness: Callable[[], tuple[bool, Mapping[str, str]]],
        operations: SharedOperationsControl | None = None,
    ) -> None:
        self._authority = authority
        self._generations = generations
        self._readiness = readiness
        self._operations = operations

    async def Authenticate(self, request, context):
        await _require_local_transport(context)
        try:
            handshake = SharedHandshake.model_validate_json(request.signed_handshake)
            credential = self._authority.authenticate(
                handshake,
                peer_uid=os.getuid(),
            )
        except (SharedAuthenticationError, ValueError) as exc:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, str(exc))
            raise AssertionError("abort returned")
        return pb.HandshakeResponse(
            session_credential=credential.model_dump_json().encode(),
            protocol_version=credential.protocol_version,
            daemon_socket_generation=credential.socket_generation,
        )

    async def Negotiate(self, request, context):
        await _require_local_transport(context)
        try:
            result = self._authority.negotiate(
                ProtocolNegotiation(
                    supported_versions=tuple(request.protocol_versions),
                    capabilities=tuple(request.capabilities),
                )
            )
        except SharedAuthenticationError as exc:
            await context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(exc))
            raise AssertionError("abort returned")
        return pb.NegotiationResponse(
            protocol_version=result.protocol_version,
            capabilities=result.capabilities,
            daemon_socket_generation=result.socket_generation,
        )

    async def StageGeneration(self, request, context):
        credential = await _credential(request.envelope, context, self._authority)
        try:
            generation_id, digest, state = await self._generations.stage_generation(
                GenerationCommand(
                    envelope=_envelope(request.envelope),
                    generation_artifact=bytes(request.generation_artifact),
                ),
                credential,
            )
        except (KeyError, ValueError) as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            raise AssertionError("abort returned")
        return pb.GenerationStatus(generation_id=generation_id, artifact_digest=digest, state=state)

    async def ObserveGeneration(self, request, context):
        credential = await _credential(request.envelope, context, self._authority)
        try:
            generation_id, digest, state = await self._generations.observe_generation(
                GenerationCommand(envelope=_envelope(request.envelope)), credential
            )
        except KeyError as exc:
            await context.abort(grpc.StatusCode.NOT_FOUND, str(exc))
            raise AssertionError("abort returned")
        except ValueError as exc:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(exc))
            raise AssertionError("abort returned")
        return pb.GenerationStatus(generation_id=generation_id, artifact_digest=digest, state=state)

    async def GetReadiness(self, request, context):
        await _require_local_transport(context)
        ready, components = self._readiness()
        return pb.Readiness(ready=ready, components=components)

    async def Backup(self, request, context):
        credential = await _credential(request.envelope, context, self._authority)
        await _require_operations_principal(context, credential)
        operations = await self._require_operations(context)
        try:
            destination = Path(request.destination)
            digest = await operations.backup(destination, request.consistency)
        except (OSError, ValueError, RuntimeError) as exc:
            await _abort_backend_error(context, exc)
            raise AssertionError("abort returned")
        return pb.BackupResult(
            destination=str(destination),
            consistency=request.consistency,
            digest=digest,
        )

    async def VerifyRestore(self, request, context):
        credential = await _credential(request.envelope, context, self._authority)
        await _require_operations_principal(context, credential)
        operations = await self._require_operations(context)
        try:
            digest = await operations.verify_restore(Path(request.source))
        except (OSError, ValueError, RuntimeError) as exc:
            await _abort_backend_error(context, exc)
            raise AssertionError("abort returned")
        return pb.VerifyRestoreResult(verified=True, digest=digest)

    async def ReconcileAll(self, request, context):
        credential = await _credential(request.envelope, context, self._authority)
        await _require_operations_principal(context, credential)
        operations = await self._require_operations(context)
        attempts, sessions = await operations.reconcile_all()
        return pb.ReconcileAllResult(attempts=attempts, sessions=sessions)

    async def BeginDrain(self, request, context):
        credential = await _credential(request.envelope, context, self._authority)
        await _require_operations_principal(context, credential)
        operations = await self._require_operations(context)
        try:
            await operations.begin_drain(timeout_seconds=request.timeout_seconds)
        except (ValueError, RuntimeError, TimeoutError) as exc:
            await _abort_backend_error(context, exc)
            raise AssertionError("abort returned")
        ready, components = self._readiness()
        return pb.Readiness(ready=ready, components=components)

    async def _require_operations(self, context) -> SharedOperationsControl:
        if self._operations is None:
            await context.abort(
                grpc.StatusCode.UNIMPLEMENTED,
                "Shared operational control is unavailable",
            )
            raise AssertionError("abort returned")
        return self._operations


class SharedGrpcServer:
    def __init__(
        self,
        *,
        socket_path: Path,
        authority: SharedHandshakeAuthority,
        backend: SharedExecutionBackend,
        generations: SharedGenerationControl,
        readiness: Callable[[], tuple[bool, Mapping[str, str]]],
        operations: SharedOperationsControl | None = None,
        max_receive_bytes: int = 16 * 1024 * 1024,
        max_send_bytes: int = 16 * 1024 * 1024,
        max_concurrent_streams: int = 1024,
    ) -> None:
        if max_concurrent_streams <= 0:
            raise ValueError("max concurrent streams must be positive")
        self._socket_path = socket_path
        self._server = grpc.aio.server(
            options=(
                ("grpc.max_receive_message_length", max_receive_bytes),
                ("grpc.max_send_message_length", max_send_bytes),
                ("grpc.max_concurrent_streams", max_concurrent_streams),
                ("grpc.max_metadata_size", 16 * 1024),
                ("grpc.keepalive_time_ms", 60_000),
                ("grpc.keepalive_timeout_ms", 10_000),
                ("grpc.http2.max_pings_without_data", 2),
            )
        )
        rpc.add_GatewayExecutionServiceServicer_to_server(
            _ExecutionService(backend, authority, readiness), self._server
        )
        rpc.add_GatewayControlServiceServicer_to_server(
            _ControlService(authority, generations, readiness=readiness, operations=operations),
            self._server,
        )
        credentials = grpc.local_server_credentials(grpc.LocalConnectionType.UDS)
        bound = self._server.add_secure_port(
            f"unix:{socket_path}",
            credentials,
        )
        if bound == 0:
            raise RuntimeError("failed to bind Shared gRPC UDS")

    async def start(self) -> None:
        await self._server.start()
        os.chmod(self._socket_path, 0o600)

    async def stop(self, *, grace_seconds: float) -> None:
        await self._server.stop(grace_seconds)

    async def wait(self) -> None:
        await self._server.wait_for_termination()


def _envelope(value: Any) -> RpcEnvelopeBinding:
    return RpcEnvelopeBinding(
        generation_id=value.generation_id,
        generation_artifact_digest=value.generation_artifact_digest,
    )


def _start(value: Any) -> StartExecutionCommand:
    return StartExecutionCommand(
        envelope=_envelope(value.envelope),
        execution_id=value.execution_id,
        operation=value.operation,
        canonical_request=bytes(value.canonical_request),
        artifact_reference=value.artifact_reference,
    )


def _query(value: Any) -> ExecutionQuery:
    return ExecutionQuery(
        envelope=_envelope(value.envelope),
        execution_id=value.execution_id,
    )


def _cursor(value: Any) -> EventCursor:
    return EventCursor(
        envelope=_envelope(value.envelope),
        execution_id=value.execution_id,
        after_sequence=value.after_sequence,
        receipt_revision=value.receipt_revision,
    )


def _session_message(value: Any) -> SessionMessageCommand:
    return SessionMessageCommand(
        envelope=_envelope(value.envelope),
        execution_id=value.execution_id,
        application_sequence=value.application_sequence,
        message=SessionApplicationMessage.model_validate_json(value.payload),
        permit=WirePermit.model_validate_json(value.wire_permit),
    )


def _event_pb(value: LifecycleEventView) -> Any:
    return pb.LifecycleEvent(
        execution_id=value.execution_id,
        sequence=value.sequence,
        receipt_revision=value.receipt_revision,
        event_type=value.event_type,
        payload=value.payload,
    )


async def _credential(
    envelope: Any,
    context: grpc.aio.ServicerContext,
    authority: SharedHandshakeAuthority,
) -> SharedSessionCredential:
    await _require_local_transport(context)
    try:
        credential = SharedSessionCredential.model_validate_json(envelope.principal_proof)
    except ValueError as exc:
        await context.abort(grpc.StatusCode.UNAUTHENTICATED, "invalid session credential")
        raise AssertionError("abort returned") from exc
    if not authority.verify_session(credential, peer_uid=os.getuid()):
        await context.abort(grpc.StatusCode.UNAUTHENTICATED, "session credential rejected")
    if envelope.schema_version != 1:
        await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "unsupported schema version")
    if envelope.protocol_version != credential.protocol_version:
        await context.abort(
            grpc.StatusCode.FAILED_PRECONDITION,
            "session protocol version does not match envelope",
        )
    return credential


async def _require_operations_principal(context: grpc.aio.ServicerContext, credential: SharedSessionCredential) -> None:
    principal = credential.principal
    if (
        principal.project_id != "gateway-operations"
        or principal.subject_id != "gateway-cli"
        or principal.policy_revision != "gateway-operations-v1"
    ):
        await context.abort(
            grpc.StatusCode.PERMISSION_DENIED,
            "session credential is not authorized for gateway operations",
        )


async def _backend_call(context: Any, operation: Awaitable[Any]) -> Any:
    try:
        return await operation
    except (KeyError, PermissionError, ValueError, RuntimeError) as exc:
        await _abort_backend_error(context, exc)
        raise AssertionError("abort returned")


async def _abort_backend_error(context: Any, exc: Exception) -> None:
    if isinstance(exc, KeyError):
        code = grpc.StatusCode.NOT_FOUND
    elif isinstance(exc, PermissionError):
        code = grpc.StatusCode.PERMISSION_DENIED
    elif isinstance(exc, ValueError):
        code = grpc.StatusCode.INVALID_ARGUMENT
    else:
        code = grpc.StatusCode.FAILED_PRECONDITION
    await context.abort(code, str(exc))


async def _require_local_transport(context: grpc.aio.ServicerContext) -> None:
    auth = context.auth_context()
    security = auth.get("transport_security_type", ())
    if b"local" not in security:
        await context.abort(
            grpc.StatusCode.UNAUTHENTICATED,
            "Shared RPC requires local UDS credentials",
        )
