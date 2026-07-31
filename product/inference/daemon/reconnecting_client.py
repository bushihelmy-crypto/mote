"""Safe Shared client rebinding across daemon socket generations."""

from __future__ import annotations

import asyncio
import os
import secrets
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Generic, Protocol, TypeVar

import grpc

from mote.contracts.inference.shared import SharedHandshake
from mote.product.inference.daemon.client_port import (
    AuthenticatingSharedRuntimeClient,
    RpcEnvelope,
    RpcGenerationStatus,
    RpcLifecycleEvent,
    RpcReceipt,
    RpcStartResponse,
    SharedRuntimeClient,
)
from mote.product.inference.daemon.security import current_incarnation, sign_handshake
from mote.product.inference.daemon.supervisor import SharedDaemonSupervisor
from mote.product.inference.security.permit_issuer import ProductWirePermitIssuer

StartRequestT = TypeVar("StartRequestT", contravariant=True)
TransferRequestT = TypeVar("TransferRequestT", contravariant=True)
SessionMessageT = TypeVar("SessionMessageT", contravariant=True)


class SharedClientFactory(Protocol[StartRequestT, TransferRequestT, SessionMessageT]):
    def __call__(
        self, socket_path: Path
    ) -> AuthenticatingSharedRuntimeClient[StartRequestT, TransferRequestT, SessionMessageT]:
        ...


class SharedReconnectAuthenticator:
    def __init__(
        self,
        *,
        protocol_versions: tuple[int, ...],
        application_id: str,
        key_id: str,
        application_key: bytes,
        tenant_id: str,
        project_id: str,
        subject_id: str,
        policy_revision: str,
        delegation_digest: str,
    ) -> None:
        self._protocol_versions = protocol_versions
        self._application_id = application_id
        self._key_id = key_id
        self._application_key = application_key
        self._tenant_id = tenant_id
        self._project_id = project_id
        self._subject_id = subject_id
        self._policy_revision = policy_revision
        self._delegation_digest = delegation_digest

    def handshake(self, socket_generation: str) -> SharedHandshake:
        now = datetime.now(timezone.utc)
        return sign_handshake(
            SharedHandshake(
                protocol_versions=self._protocol_versions,
                application_id=self._application_id,
                caller=current_incarnation(os.getpid()),
                socket_generation=socket_generation,
                tenant_id=self._tenant_id,
                project_id=self._project_id,
                subject_id=self._subject_id,
                policy_revision=self._policy_revision,
                delegation_digest=self._delegation_digest,
                nonce=secrets.token_urlsafe(24),
                issued_at=now,
                expires_at=now + timedelta(seconds=30),
                key_id=self._key_id,
                signature="unsigned",
            ),
            self._application_key,
        )


class ReconnectingSharedGrpcClient(Generic[StartRequestT, TransferRequestT, SessionMessageT]):
    """Retries only receipt/cursor reads, never a business mutation."""

    def __init__(
        self,
        supervisor: SharedDaemonSupervisor,
        authenticator: SharedReconnectAuthenticator,
        client_factory: SharedClientFactory[StartRequestT, TransferRequestT, SessionMessageT],
    ) -> None:
        self._supervisor = supervisor
        self._authenticator = authenticator
        self._client_factory = client_factory
        self._client: SharedRuntimeClient[StartRequestT, TransferRequestT, SessionMessageT] | None = None
        self._socket_generation: str | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    async def connect(self) -> None:
        async with self._lock:
            await self._connect_locked(force=False)

    def envelope(self, **fields: object) -> RpcEnvelope:
        return self._require_client().envelope(**fields)

    def permit_issuer(self) -> ProductWirePermitIssuer:
        return self._require_client().permit_issuer()

    async def stage_generation(
        self, artifact: bytes, *, generation_id: str, artifact_digest: str
    ) -> RpcGenerationStatus:
        return await self._require_client().stage_generation(
            artifact, generation_id=generation_id, artifact_digest=artifact_digest
        )

    async def start_unary(self, request: StartRequestT, *, timeout: float | None = None) -> RpcStartResponse:
        return await self._require_client().start_unary(request, timeout=timeout)

    async def start_durable_command(self, request: StartRequestT, *, timeout: float | None = None) -> RpcStartResponse:
        return await self._require_client().start_durable_command(request, timeout=timeout)

    async def open_session(self, request: StartRequestT, *, timeout: float | None = None) -> RpcStartResponse:
        return await self._require_client().open_session(request, timeout=timeout)

    async def execute_transfer_part(
        self, request: TransferRequestT, *, timeout: float | None = None
    ) -> RpcStartResponse:
        return await self._require_client().execute_transfer_part(request, timeout=timeout)

    async def authorize_wire(
        self,
        execution_id: str,
        wire_permit: bytes,
        *,
        generation_id: str,
        timeout: float | None = None,
    ) -> None:
        await self._require_client().authorize_wire(
            execution_id, wire_permit, generation_id=generation_id, timeout=timeout
        )

    async def cancel(
        self,
        execution_id: str,
        reason: str,
        *,
        generation_id: str,
        timeout: float | None = None,
    ) -> None:
        await self._require_client().cancel(execution_id, reason, generation_id=generation_id, timeout=timeout)

    def session(self, requests: AsyncIterator[SessionMessageT]) -> AsyncIterator[RpcLifecycleEvent]:
        return self._require_client().session(requests)

    async def query_receipt(
        self,
        execution_id: str,
        *,
        generation_id: str,
        timeout: float | None = None,
    ) -> RpcReceipt:
        deadline = self._deadline(timeout)
        client = self._require_client()
        try:
            return await client.query_receipt(
                execution_id,
                generation_id=generation_id,
                timeout=self._remaining(deadline),
            )
        except grpc.aio.AioRpcError:
            client = await self._reconnect()
            return await client.query_receipt(
                execution_id,
                generation_id=generation_id,
                timeout=self._remaining(deadline),
            )

    async def resume_events(
        self,
        execution_id: str,
        *,
        generation_id: str,
        after_sequence: int,
        receipt_revision: int,
        timeout: float | None = None,
    ) -> AsyncIterator[RpcLifecycleEvent]:
        deadline = self._deadline(timeout)
        cursor = after_sequence
        revision = receipt_revision
        client = self._require_client()
        try:
            async for event in client.resume_events(
                execution_id,
                generation_id=generation_id,
                after_sequence=cursor,
                receipt_revision=revision,
                timeout=self._remaining(deadline),
            ):
                cursor = max(cursor, event.sequence)
                revision = max(revision, event.receipt_revision)
                yield event
            return
        except grpc.aio.AioRpcError:
            client = await self._reconnect()
        async for event in client.resume_events(
            execution_id,
            generation_id=generation_id,
            after_sequence=cursor,
            receipt_revision=revision,
            timeout=self._remaining(deadline),
        ):
            yield event

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            client = self._client
            self._client = None
        if client is not None:
            await client.close()

    async def _reconnect(
        self,
    ) -> SharedRuntimeClient[StartRequestT, TransferRequestT, SessionMessageT]:
        async with self._lock:
            return await self._connect_locked(force=True)

    async def _connect_locked(
        self, *, force: bool
    ) -> SharedRuntimeClient[StartRequestT, TransferRequestT, SessionMessageT]:
        if self._closed:
            raise RuntimeError("Shared reconnecting client is closed")
        discovery, socket_path = self._supervisor.discover_ready_socket()
        if not force and self._client is not None and self._socket_generation == discovery.socket_generation:
            return self._client
        candidate = self._client_factory(socket_path)
        handshake = self._authenticator.handshake(discovery.socket_generation)
        try:
            negotiated = await candidate.authenticate(handshake)
            if negotiated.socket_generation != discovery.socket_generation:
                raise RuntimeError("Shared discovery changed during authentication")
        except BaseException:
            await candidate.close()
            raise
        previous = self._client
        self._client = candidate
        self._socket_generation = discovery.socket_generation
        if previous is not None:
            await previous.close()
        return candidate

    def _require_client(
        self,
    ) -> SharedRuntimeClient[StartRequestT, TransferRequestT, SessionMessageT]:
        if self._client is None:
            raise RuntimeError("Shared reconnecting client is not connected")
        return self._client

    @staticmethod
    def _deadline(timeout: float | None) -> float | None:
        if timeout is None:
            return None
        if timeout <= 0:
            raise TimeoutError("Shared RPC deadline expired")
        return asyncio.get_running_loop().time() + timeout

    @staticmethod
    def _remaining(deadline: float | None) -> float | None:
        if deadline is None:
            return None
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("Shared RPC deadline expired during reconnect")
        return remaining


__all__ = ["ReconnectingSharedGrpcClient", "SharedReconnectAuthenticator"]
