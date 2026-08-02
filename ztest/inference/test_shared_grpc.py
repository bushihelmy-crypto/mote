import asyncio
import os
from datetime import datetime, timedelta, timezone

import grpc
import pytest

from mote.contracts.inference.generation_artifact import (
    CapabilityPricingSnapshot,
    DeploymentKind,
    GenerationActivationPolicy,
    GenerationArtifact,
    ModelGenerationBinding,
    RuntimeBindingKind,
    ServiceGenerationBinding,
    SessionGenerationBinding,
    TransferGenerationBinding,
    VersionBinding,
    compute_generation_artifact_digest,
)
from mote.contracts.inference.shared import SharedHandshake
from mote.product.inference.daemon.generation import SharedGenerationBackend
from mote.product.inference.daemon.grpc_client import SharedGrpcClient
from mote.product.inference.daemon.grpc_server import SharedGrpcServer
from mote.product.inference.daemon.rpc import gateway_v1_pb2 as pb
from mote.product.inference.daemon.security import SharedHandshakeAuthority, current_incarnation, sign_handshake
from mote.runtime.inference.generation import GatewayGenerationOwner

DIGEST = "sha256:" + "8" * 64


class _Backend:
    def __init__(self):
        self.credential = None

    async def start_unary(self, request, credential):
        self.credential = credential
        return 7

    async def query_receipt(self, request, credential):
        self.credential = credential
        if request.execution_id == "missing":
            raise KeyError("unknown execution")
        return pb.Receipt(execution_id=request.execution_id, revision=9, state="RUNNING")

    async def stream_events(self, request, credential):
        self.credential = credential
        yield pb.LifecycleEvent(
            execution_id=request.execution_id,
            sequence=request.after_sequence,
            receipt_revision=9,
            event_type="duplicate",
        )
        yield pb.LifecycleEvent(
            execution_id=request.execution_id,
            sequence=request.after_sequence + 2,
            receipt_revision=11,
            event_type="reordered",
        )
        yield pb.LifecycleEvent(
            execution_id=request.execution_id,
            sequence=request.after_sequence + 1,
            receipt_revision=10,
            event_type="progress",
        )


def _authority():
    return SharedHandshakeAuthority(
        socket_generation="socket-generation",
        application_keys={"application": ("application-key", b"application-secret")},
        session_key_id="session-key",
        session_key=b"session-secret",
        current_protocol_version=3,
    )


def _handshake(*, signature_key=b"application-secret"):
    now = datetime.now(timezone.utc)
    unsigned = SharedHandshake(
        protocol_versions=(3, 2),
        application_id="application",
        caller=current_incarnation(os.getpid()),
        socket_generation="socket-generation",
        tenant_id="tenant",
        project_id="project",
        subject_id="subject",
        policy_revision="policy-1",
        delegation_digest=DIGEST,
        nonce=os.urandom(16).hex(),
        issued_at=now,
        expires_at=now + timedelta(seconds=30),
        key_id="application-key",
        signature="unsigned",
    )
    return sign_handshake(unsigned, signature_key)


def _artifact():
    artifact = GenerationArtifact(
        generation_id="model-generation",
        model_binding=ModelGenerationBinding(topology_revision="topology"),
        service_binding=ServiceGenerationBinding(runtime=RuntimeBindingKind.SHARED_RPC, configured=True),
        session_binding=SessionGenerationBinding(runtime=RuntimeBindingKind.SHARED_RPC, configured=True),
        transfer_binding=TransferGenerationBinding(runtime=RuntimeBindingKind.SHARED_RPC, configured=True),
        credential_versions=(),
        transport_registry_revision="transport-v1",
        client_profile_revision="canonical-v1",
        failure_policy_revision="failure-v2",
        capability_pricing=CapabilityPricingSnapshot(catalog_revision="catalog", pricing_revision="pricing"),
        governance_plugins=(),
        required_wire_contract_range=(2, 3),
        activation_policy=GenerationActivationPolicy(
            deployment=DeploymentKind.SHARED_PROCESS, activate_immediately=True
        ),
        min_reader_version=1,
        min_writer_version=1,
        persistence_schemas=(VersionBinding(identity="receipt", revision="1"),),
        migration_set_digest="sha256:" + "7" * 64,
        artifact_digest="sha256:" + "9" * 64,
    )
    return artifact.model_copy(update={"artifact_digest": compute_generation_artifact_digest(artifact)})


def test_shared_uds_authentication_start_and_cursor_resume(tmp_path):
    async def scenario():
        backend = _Backend()
        accepting = True
        socket_path = tmp_path / "gateway.sock"

        def readiness():
            return accepting, {"admission": "ready" if accepting else "draining"}

        server = SharedGrpcServer(
            socket_path=socket_path,
            authority=_authority(),
            backend=backend,
            generations=SharedGenerationBackend(GatewayGenerationOwner()),
            readiness=readiness,
        )
        await server.start()
        client = SharedGrpcClient(socket_path)
        try:
            negotiated = await client.authenticate(_handshake(), capabilities=("events",))
            assert negotiated.protocol_version == 3
            assert negotiated.capabilities == ("events",)
            artifact = _artifact()
            staged = await client.stage_generation(
                artifact.model_dump_json().encode(),
                generation_id=artifact.generation_id,
                artifact_digest=artifact.artifact_digest,
            )
            assert staged.state == "active"
            observed = await client.observe_generation(artifact.generation_id)
            assert observed.artifact_digest == artifact.artifact_digest
            readiness = await client.get_readiness()
            assert readiness.ready is True
            assert readiness.components == {"admission": "ready"}
            response = await client.start_unary(
                pb.StartRequest(
                    envelope=client.envelope(idempotency_key="start:execution"),
                    execution_id="execution",
                    operation="chat",
                )
            )
            assert response.receipt_revision == 7
            events = [
                event
                async for event in client.resume_events(
                    "execution",
                    generation_id="model-generation",
                    after_sequence=4,
                    receipt_revision=8,
                )
            ]
            assert [(event.sequence, event.event_type) for event in events] == [
                (5, "progress"),
                (6, "reordered"),
            ]
            assert backend.credential.principal.tenant_id == "tenant"
            assert (socket_path.stat().st_mode & 0o777) == 0o600
            with pytest.raises(grpc.aio.AioRpcError) as missing:
                await client.query_receipt("missing", generation_id="model-generation")
            assert missing.value.code() is grpc.StatusCode.NOT_FOUND
            accepting = False
            with pytest.raises(grpc.aio.AioRpcError) as draining:
                await client.start_unary(
                    pb.StartRequest(
                        envelope=client.envelope(idempotency_key="start:rejected"),
                        execution_id="rejected",
                    )
                )
            assert draining.value.code() is grpc.StatusCode.UNAVAILABLE
        finally:
            await client.close()
            await server.stop(grace_seconds=0)

    asyncio.run(scenario())


def test_shared_uds_rejects_bad_application_signature(tmp_path):
    async def scenario():
        socket_path = tmp_path / "gateway.sock"
        server = SharedGrpcServer(
            socket_path=socket_path,
            authority=_authority(),
            backend=_Backend(),
            generations=SharedGenerationBackend(GatewayGenerationOwner()),
            readiness=lambda: (True, {}),
        )
        await server.start()
        client = SharedGrpcClient(socket_path)
        try:
            with pytest.raises(grpc.aio.AioRpcError) as captured:
                await client.authenticate(_handshake(signature_key=b"wrong-secret"))
            assert captured.value.code() is grpc.StatusCode.UNAUTHENTICATED
        finally:
            await client.close()
            await server.stop(grace_seconds=0)

    asyncio.run(scenario())
