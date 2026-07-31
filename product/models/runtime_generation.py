"""Product assembly for one generation-owned Embedded inference data plane."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mote.contracts.config.inference import DeploymentMode, InferenceConfig
from mote.contracts.inference.generation_artifact import GenerationArtifact
from mote.contracts.inference.identity import InferencePrincipal, TrustedSchedulingClass
from mote.contracts.inference.shared import SharedHandshake
from mote.contracts.ports.inference.provider_transport import GenerateTransport
from mote.product.inference.backends.sqlite import (
    SQLiteAttemptReceiptStore,
    SQLiteSessionReceiptStore,
    SQLiteUsageLedger,
)
from mote.product.inference.daemon.grpc_client import SharedGrpcClient
from mote.product.inference.daemon.reconnecting_client import ReconnectingSharedGrpcClient, SharedReconnectAuthenticator
from mote.product.inference.daemon.security import current_incarnation, sign_handshake
from mote.product.inference.daemon.shared_runtime import (
    SharedArtifactTransferRuntime,
    SharedInferenceRuntime,
    SharedServiceCommandRuntime,
    SharedSessionRuntime,
)
from mote.product.inference.daemon.supervisor import SharedDaemonSupervisor
from mote.product.inference.security.permit_issuer import ProductWirePermitIssuer
from mote.product.inference.security.wire_permit import Ed25519WirePermitSigner, Ed25519WirePermitVerifier
from mote.product.models.bindings import ProductModelBindingResolver
from mote.product.models.compiler import CompiledModelGeneration
from mote.product.models.secrets import CredentialWireAccess, SecretHandle
from mote.product.models.transports import (
    AnthropicMessagesTransport,
    BedrockAnthropicTransport,
    GoogleFiniteTransport,
    GoogleGenerateContentTransport,
    OpenAIChatTransport,
    OpenAIFiniteTransport,
    OpenAIOperationTransport,
    OpenAIRealtimeTransport,
    OpenAIResponsesTransport,
    ProductFiniteTransportResolver,
    ProductOperationTransportResolver,
    ProductSessionTransportResolver,
)
from mote.product.models.transports.artifact_io import ArtifactPublisher, ArtifactResolver
from mote.product.models.transports.bedrock import AwsCredentials
from mote.product.models.transports.connections.aiohttp import AioHttpConnectionPool, ConnectionConfig
from mote.runtime.inference.command_runtime import EmbeddedServiceCommandRuntime
from mote.runtime.inference.generation import GatewayGenerationOwner
from mote.runtime.inference.governance import CredentialHealthAuthority, ProviderQuotaAuthority
from mote.runtime.inference.runtime import EmbeddedInferenceRuntime
from mote.runtime.inference.session_runtime import EmbeddedSessionRuntime
from mote.runtime.inference.transfer_runtime import EmbeddedArtifactTransferRuntime
from mote.runtime.models.failover.planner import FailoverPlanner
from mote.runtime.models.failover.runtime_state import ModelRuntimeGeneration
from mote.runtime.models.failover.snapshot import build_canonical_model_runtime_snapshot
from mote.runtime.models.generation_lifecycle import GenerationLifecycle
from mote.runtime.models.inference_attempt_executor import InferenceAttemptExecutor


async def build_embedded_model_runtime_generation(
    compiled: CompiledModelGeneration,
    config: InferenceConfig,
    *,
    state_root: Path,
    artifact_resolver: ArtifactResolver | None = None,
    artifact_publisher: ArtifactPublisher | None = None,
) -> ModelRuntimeGeneration:
    pool = AioHttpConnectionPool()
    try:
        return await _build_embedded_model_runtime_generation(
            compiled,
            config,
            state_root=state_root,
            pool=pool,
            artifact_resolver=artifact_resolver,
            artifact_publisher=artifact_publisher,
        )
    except BaseException as primary:
        errors: list[BaseException] = []
        try:
            await pool.aclose()
        except BaseException as exc:
            errors.append(exc)
        errors.extend(await _close_handles(compiled))
        if errors:
            raise BaseExceptionGroup("model generation readiness cleanup failed", errors) from primary
        raise


async def build_model_runtime_generation(
    compiled: CompiledModelGeneration,
    config: InferenceConfig,
    *,
    state_root: Path,
    artifact_resolver: ArtifactResolver | None = None,
    artifact_publisher: ArtifactPublisher | None = None,
) -> ModelRuntimeGeneration:
    if config.deployment is DeploymentMode.EMBEDDED:
        return await build_embedded_model_runtime_generation(
            compiled,
            config,
            state_root=state_root,
            artifact_resolver=artifact_resolver,
            artifact_publisher=artifact_publisher,
        )
    return await _build_shared_model_runtime_generation(compiled, config, state_root=state_root)


async def _build_shared_model_runtime_generation(
    compiled: CompiledModelGeneration,
    config: InferenceConfig,
    *,
    state_root: Path,
) -> ModelRuntimeGeneration:
    shared = config.shared_process
    if shared is None:
        raise ValueError("Shared Process configuration is required")
    runtime_directory = state_root / shared.runtime_directory
    supervisor = SharedDaemonSupervisor(
        runtime_directory,
        protocol_version=max(shared.rpc_contract_versions),
    )
    _discovery, socket_path = supervisor.discover_ready_socket()
    initial_client = SharedGrpcClient(
        socket_path,
        max_receive_bytes=config.compatibility.max_body_bytes,
        max_send_bytes=config.compatibility.max_body_bytes,
    )
    try:
        negotiation = await initial_client.negotiate(shared.rpc_contract_versions)
        application_id, key_id, key = _shared_application_identity(state_root)
        snapshot = build_canonical_model_runtime_snapshot(compiled.topology)
        now = datetime.now(timezone.utc)
        principal = InferencePrincipal(
            tenant_id="mote-application",
            project_id="model-runtime",
            subject_id="product-composition",
            policy_revision=snapshot.revision,
            delegation_digest="sha256:" + hashlib.sha256(snapshot.revision.encode()).hexdigest(),
        )
        handshake = sign_handshake(
            SharedHandshake(
                protocol_versions=shared.rpc_contract_versions,
                application_id=application_id,
                caller=current_incarnation(os.getpid()),
                socket_generation=negotiation.socket_generation,
                tenant_id=principal.tenant_id,
                project_id=principal.project_id,
                subject_id=principal.subject_id,
                policy_revision=principal.policy_revision,
                delegation_digest=principal.delegation_digest,
                nonce=secrets.token_urlsafe(24),
                issued_at=now,
                expires_at=now + timedelta(seconds=30),
                key_id=key_id,
                signature="unsigned",
            ),
            key,
        )
        await initial_client.authenticate(handshake)
        await initial_client.close()
        authenticator = SharedReconnectAuthenticator(
            protocol_versions=shared.rpc_contract_versions,
            application_id=application_id,
            key_id=key_id,
            application_key=key,
            tenant_id=principal.tenant_id,
            project_id=principal.project_id,
            subject_id=principal.subject_id,
            policy_revision=principal.policy_revision,
            delegation_digest=principal.delegation_digest,
        )
        client = ReconnectingSharedGrpcClient[object, object, object](
            supervisor,
            authenticator,
            lambda socket_path: SharedGrpcClient(
                socket_path,
                max_receive_bytes=config.compatibility.max_body_bytes,
                max_send_bytes=config.compatibility.max_body_bytes,
            ),
        )
        await client.connect()
        generation_id = f"model-{uuid4().hex}"
        artifact_digest = "sha256:" + hashlib.sha256(f"{generation_id}\0{snapshot.revision}".encode()).hexdigest()
        artifact = _generation_artifact(
            generation_id,
            artifact_digest,
            snapshot.revision,
            compiled,
            deployment="shared_process",
            activate_immediately=True,
        )
        status = await client.stage_generation(
            artifact.model_dump_json().encode(),
            generation_id=generation_id,
            artifact_digest=artifact_digest,
        )
        if status.generation_id != generation_id or status.artifact_digest != artifact_digest:
            raise RuntimeError("Shared daemon staged a different generation")
        runtime = SharedInferenceRuntime(client, owns_client=False)
        commands = SharedServiceCommandRuntime(client, owns_client=False)
        sessions = SharedSessionRuntime(client, owns_client=False)
        transfers = SharedArtifactTransferRuntime(client, owns_client=False)
        lifecycle = GenerationLifecycle(
            (
                runtime,
                commands,
                sessions,
                transfers,
                client,
                *compiled.credential_bindings.handles.values(),
            )
        )
        permit_issuer = client.permit_issuer()
        permit_audience = f"shared/{negotiation.socket_generation}/model/{principal.tenant_id}"
        return ModelRuntimeGeneration(
            planner=FailoverPlanner(snapshot),
            binding_resolver=ProductModelBindingResolver(compiled.credential_bindings),
            attempt_executor=InferenceAttemptExecutor(
                runtime,
                permit_issuer,
                permit_audience=permit_audience,
                epoch_provider=lambda: (0, 0),
            ),
            command_runtime=commands,
            session_runtime=sessions,
            transfer_runtime=transfers,
            permit_issuer=permit_issuer,
            permit_audience=permit_audience,
            generation_id=generation_id,
            generation_artifact_digest=artifact_digest,
            principal=principal,
            scheduling=TrustedSchedulingClass(),
            closeables=(lifecycle,),
        )
    except BaseException as primary:
        errors: list[BaseException] = []
        try:
            if "client" in locals():
                await client.close()
            else:
                await initial_client.close()
        except BaseException as exc:
            errors.append(exc)
        errors.extend(await _close_handles(compiled))
        if errors:
            raise BaseExceptionGroup("Shared model generation readiness cleanup failed", errors) from primary
        raise


async def _close_handles(
    compiled: CompiledModelGeneration,
) -> list[BaseException]:
    errors: list[BaseException] = []
    closed: set[int] = set()
    for handle in compiled.credential_bindings.handles.values():
        if id(handle) in closed:
            continue
        closed.add(id(handle))
        try:
            await handle.aclose()
        except BaseException as exc:
            errors.append(exc)
    return errors


async def _build_embedded_model_runtime_generation(
    compiled: CompiledModelGeneration,
    config: InferenceConfig,
    *,
    state_root: Path,
    pool: AioHttpConnectionPool,
    artifact_resolver: ArtifactResolver | None,
    artifact_publisher: ArtifactPublisher | None,
) -> ModelRuntimeGeneration:
    snapshot = build_canonical_model_runtime_snapshot(compiled.topology)
    planner = FailoverPlanner(snapshot)
    binding_resolver = ProductModelBindingResolver(compiled.credential_bindings)
    transports = await _build_transports(compiled, snapshot.endpoints, config, pool)
    finite_transports = await _build_finite_transports(
        compiled,
        snapshot.endpoints,
        config,
        pool,
        artifact_resolver=artifact_resolver,
        artifact_publisher=artifact_publisher,
    )
    operation_transports, session_transports = await _build_operation_transports(
        compiled,
        snapshot.endpoints,
        config,
        pool,
        artifact_resolver=artifact_resolver,
        artifact_publisher=artifact_publisher,
    )

    database = state_root / "inference" / "gateway.sqlite3"
    receipts = SQLiteAttemptReceiptStore(
        database,
        busy_timeout_seconds=config.persistence.shared_sqlite.busy_timeout_seconds,
    )
    await receipts.initialize()
    if config.persistence.shared_sqlite.quick_check_on_start:
        await receipts.verify_startup(hard_min_free_bytes=config.persistence.shared_sqlite.hard_disk_free_bytes)
    await receipts.reconcile_incomplete()
    usage = SQLiteUsageLedger(receipts)
    principal = InferencePrincipal(
        tenant_id="mote-application",
        project_id="model-runtime",
        subject_id="product-composition",
        policy_revision=snapshot.revision,
        delegation_digest="sha256:" + hashlib.sha256(snapshot.revision.encode()).hexdigest(),
    )
    await usage.configure_budget(
        principal.tenant_id,
        principal.project_id,
        (1 << 62) - 1,
    )

    generation_id = f"model-{uuid4().hex}"
    artifact_digest = "sha256:" + hashlib.sha256(f"{generation_id}\0{snapshot.revision}".encode()).hexdigest()
    artifact = _generation_artifact(
        generation_id,
        artifact_digest,
        snapshot.revision,
        compiled,
        service_configured=bool(operation_transports),
        session_configured=bool(session_transports),
        transfer_configured=(bool(operation_transports) and artifact_resolver is not None),
    )
    generations = GatewayGenerationOwner()
    generations.stage(artifact)
    generations.activate(generation_id, artifact_digest)

    private_key = Ed25519PrivateKey.generate()
    issuer_key_id = f"embedded:{generation_id}"
    signer = Ed25519WirePermitSigner(
        issuer_key_id=issuer_key_id,
        trust_revision=1,
        private_key=private_key,
    )
    issuer = ProductWirePermitIssuer(
        signer,
        issuer_key_id=issuer_key_id,
        trust_revision=1,
    )
    verifier = Ed25519WirePermitVerifier({(issuer_key_id, 1): private_key.public_key()})
    audience = f"embedded/{generation_id}/model/{principal.tenant_id}"
    capacity = config.capacity
    runtime = EmbeddedInferenceRuntime(
        receipts=receipts,
        usage_ledger=usage,
        reserve_units=lambda request: max(request.endpoint.execution_policy.max_output_tokens, 1),
        provider_quota=ProviderQuotaAuthority(),
        credential_health=CredentialHealthAuthority(),
        permit_verifier=verifier,
        transports=ProductFiniteTransportResolver(transports, finite_transports),
        generations=generations,
        permit_audience=audience,
        epoch_provider=lambda: (0, 0),
        queue_capacity=capacity.queue_capacity,
        event_capacity=capacity.event_buffer_capacity,
        worker_count=min(capacity.global_in_flight, 64),
        global_in_flight=capacity.global_in_flight,
        provider_in_flight=capacity.provider_in_flight,
        endpoint_in_flight=capacity.endpoint_in_flight,
        clock_skew_guard_seconds=config.deadline.clock_skew_guard_seconds,
    )
    attempt_executor = InferenceAttemptExecutor(
        runtime,
        issuer,
        permit_audience=audience,
        epoch_provider=lambda: (0, 0),
    )
    runtime_limits = {
        "queue_capacity": capacity.queue_capacity,
        "event_capacity": capacity.event_buffer_capacity,
        "worker_count": min(capacity.global_in_flight, 64),
        "global_in_flight": capacity.global_in_flight,
        "provider_in_flight": capacity.provider_in_flight,
        "endpoint_in_flight": capacity.endpoint_in_flight,
    }
    operation_resolver = ProductOperationTransportResolver(operation_transports)
    command_runtime = (
        EmbeddedServiceCommandRuntime(
            receipts=receipts,
            usage_ledger=usage,
            reserve_units=lambda _request: 1,
            provider_quota=ProviderQuotaAuthority(),
            credential_health=CredentialHealthAuthority(),
            permit_verifier=verifier,
            transports=operation_resolver,
            generations=generations,
            permit_audience=audience,
            epoch_provider=lambda: (0, 0),
            clock_skew_guard_seconds=config.deadline.clock_skew_guard_seconds,
            **runtime_limits,
        )
        if operation_transports
        else None
    )
    session_runtime = (
        EmbeddedSessionRuntime(
            session_receipts=SQLiteSessionReceiptStore(receipts),
            wire_receipts=receipts,
            usage_ledger=usage,
            reserve_open_units=lambda _request: 1,
            reserve_message_units=lambda _message: 1,
            provider_quota=ProviderQuotaAuthority(),
            credential_health=CredentialHealthAuthority(),
            permit_verifier=verifier,
            transports=ProductSessionTransportResolver(session_transports),
            generations=generations,
            permit_audience=audience,
            epoch_provider=lambda: (0, 0),
            **runtime_limits,
        )
        if session_transports
        else None
    )
    transfer_runtime = (
        EmbeddedArtifactTransferRuntime(
            receipts=receipts,
            usage_ledger=usage,
            reserve_units=lambda _request: 1,
            provider_quota=ProviderQuotaAuthority(),
            credential_health=CredentialHealthAuthority(),
            permit_verifier=verifier,
            transports=operation_resolver,
            generations=generations,
            permit_audience=audience,
            epoch_provider=lambda: (0, 0),
            clock_skew_guard_seconds=config.deadline.clock_skew_guard_seconds,
            **runtime_limits,
        )
        if operation_transports and artifact_resolver is not None
        else None
    )
    lifecycle = GenerationLifecycle(
        (
            runtime,
            *((command_runtime,) if command_runtime is not None else ()),
            *((session_runtime,) if session_runtime is not None else ()),
            *((transfer_runtime,) if transfer_runtime is not None else ()),
            pool,
            *compiled.credential_bindings.handles.values(),
        )
    )
    return ModelRuntimeGeneration(
        planner=planner,
        binding_resolver=binding_resolver,
        attempt_executor=attempt_executor,
        command_runtime=command_runtime,
        session_runtime=session_runtime,
        transfer_runtime=transfer_runtime,
        permit_issuer=issuer,
        permit_audience=audience,
        generation_id=generation_id,
        generation_artifact_digest=artifact_digest,
        principal=principal,
        scheduling=TrustedSchedulingClass(),
        closeables=(lifecycle,),
    )


async def _build_transports(
    compiled: CompiledModelGeneration,
    endpoints,
    config: InferenceConfig,
    pool: AioHttpConnectionPool,
) -> dict[tuple[str, str], GenerateTransport]:
    result: dict[tuple[str, str], GenerateTransport] = {}
    topology_endpoints = {endpoint.endpoint_id: endpoint for endpoint in compiled.topology.endpoints}
    network = config.network
    compatibility = config.compatibility
    for slot_id, handle in compiled.credential_bindings.handles.items():
        endpoint = next(
            endpoint for endpoint in endpoints if slot_id in topology_endpoints[endpoint.endpoint_id].credential_slots
        )
        connection = await pool.acquire(
            ConnectionConfig(
                fingerprint=(f"{endpoint.base_url_identity}|{endpoint.transport}|" f"{network.model_dump_json()}"),
                connection_limit=config.capacity.endpoint_in_flight,
                allow_private_network=network.allow_private_network,
                allowed_cidrs=network.allowed_cidrs,
                allowed_dns_suffixes=network.allowed_dns_suffixes,
            )
        )
        transport = endpoint.transport.lower()
        if transport in {"anthropic", "anthropic_messages"}:
            instance = AnthropicMessagesTransport(
                base_url=endpoint.base_url_identity,
                connection=connection,
                auth_headers=_auth_headers(handle, endpoint.endpoint_id, slot_id, "anthropic"),
                max_response_bytes=compatibility.max_body_bytes,
                max_stream_frame_bytes=compatibility.max_stream_frame_bytes,
            )
        elif transport in {"google", "gemini", "google_generate_content"}:
            instance = GoogleGenerateContentTransport(
                base_url=endpoint.base_url_identity,
                connection=connection,
                auth_headers=_auth_headers(handle, endpoint.endpoint_id, slot_id, "google"),
                max_response_bytes=compatibility.max_body_bytes,
                max_stream_frame_bytes=compatibility.max_stream_frame_bytes,
            )
        elif transport in {"bedrock", "aws_bedrock"}:
            instance = BedrockAnthropicTransport(
                base_url=endpoint.base_url_identity,
                region=endpoint.region,
                connection=connection,
                credentials=_aws_credentials(handle, endpoint.endpoint_id, slot_id),
                max_response_bytes=compatibility.max_body_bytes,
                max_stream_frame_bytes=compatibility.max_stream_frame_bytes,
            )
        elif transport == "openai_responses":
            instance = OpenAIResponsesTransport(
                base_url=endpoint.base_url_identity,
                connection=connection,
                auth_headers=_auth_headers(handle, endpoint.endpoint_id, slot_id, "bearer"),
                max_response_bytes=compatibility.max_body_bytes,
                max_stream_frame_bytes=compatibility.max_stream_frame_bytes,
            )
        else:
            instance = OpenAIChatTransport(
                base_url=endpoint.base_url_identity,
                connection=connection,
                auth_headers=_auth_headers(handle, endpoint.endpoint_id, slot_id, "bearer"),
                max_response_bytes=compatibility.max_body_bytes,
                max_stream_frame_bytes=compatibility.max_stream_frame_bytes,
                max_precommit_bytes=compatibility.max_precommit_bytes,
                max_precommit_frames=compatibility.max_precommit_frames,
                max_precommit_seconds=compatibility.max_precommit_seconds,
            )
        result[(endpoint.transport, slot_id)] = instance
    return result


async def _build_operation_transports(
    compiled: CompiledModelGeneration,
    endpoints,
    config: InferenceConfig,
    pool: AioHttpConnectionPool,
    *,
    artifact_resolver: ArtifactResolver | None,
    artifact_publisher: ArtifactPublisher | None,
):
    operations = {}
    sessions = {}
    topology_endpoints = {endpoint.endpoint_id: endpoint for endpoint in compiled.topology.endpoints}
    network = config.network
    for slot_id, handle in compiled.credential_bindings.handles.items():
        endpoint = next(
            endpoint for endpoint in endpoints if slot_id in topology_endpoints[endpoint.endpoint_id].credential_slots
        )
        transport = endpoint.transport.lower()
        if transport not in {
            "openai",
            "openai_chat",
            "openai_responses",
            "azure",
            "openrouter",
            "vllm",
            "xai",
        }:
            continue
        connection_config = ConnectionConfig(
            fingerprint=(
                f"{endpoint.base_url_identity}|{endpoint.transport}|operations|" f"{network.model_dump_json()}"
            ),
            connection_limit=config.capacity.endpoint_in_flight,
            allow_private_network=network.allow_private_network,
            allowed_cidrs=network.allowed_cidrs,
            allowed_dns_suffixes=network.allowed_dns_suffixes,
        )
        operations[(endpoint.endpoint_id, slot_id)] = OpenAIOperationTransport(
            endpoint_id=endpoint.endpoint_id,
            credential_slot_id=slot_id,
            base_url=endpoint.base_url_identity,
            connection=await pool.acquire(connection_config),
            auth_headers=_auth_headers(handle, endpoint.endpoint_id, slot_id, "bearer"),
            artifact_resolver=artifact_resolver,
            artifact_publisher=artifact_publisher,
            max_response_bytes=config.compatibility.max_body_bytes,
            max_upload_bytes=config.compatibility.max_body_bytes,
        )
        sessions[(endpoint.endpoint_id, slot_id)] = OpenAIRealtimeTransport(
            endpoint_id=endpoint.endpoint_id,
            base_url=endpoint.base_url_identity,
            connection=await pool.acquire(connection_config),
            auth_headers=_auth_headers(handle, endpoint.endpoint_id, slot_id, "bearer"),
            max_frame_bytes=config.compatibility.max_stream_frame_bytes,
        )
    return operations, sessions


async def _build_finite_transports(
    compiled: CompiledModelGeneration,
    endpoints,
    config: InferenceConfig,
    pool: AioHttpConnectionPool,
    *,
    artifact_resolver: ArtifactResolver | None,
    artifact_publisher: ArtifactPublisher | None,
):
    result = {}
    topology_endpoints = {endpoint.endpoint_id: endpoint for endpoint in compiled.topology.endpoints}
    network = config.network
    for slot_id, handle in compiled.credential_bindings.handles.items():
        endpoint = next(
            endpoint for endpoint in endpoints if slot_id in topology_endpoints[endpoint.endpoint_id].credential_slots
        )
        transport = endpoint.transport.lower()
        if transport not in {
            "openai",
            "openai_chat",
            "openai_responses",
            "azure",
            "openrouter",
            "vllm",
            "xai",
            "google",
            "gemini",
            "google_generate_content",
            "vertex",
        }:
            continue
        connection = await pool.acquire(
            ConnectionConfig(
                fingerprint=(
                    f"{endpoint.base_url_identity}|{endpoint.transport}|finite|" f"{network.model_dump_json()}"
                ),
                connection_limit=config.capacity.endpoint_in_flight,
                allow_private_network=network.allow_private_network,
                allowed_cidrs=network.allowed_cidrs,
                allowed_dns_suffixes=network.allowed_dns_suffixes,
            )
        )
        if transport in {
            "google",
            "gemini",
            "google_generate_content",
            "vertex",
        }:
            result[(endpoint.transport, slot_id)] = GoogleFiniteTransport(
                base_url=endpoint.base_url_identity,
                connection=connection,
                auth_headers=_auth_headers(handle, endpoint.endpoint_id, slot_id, "google"),
                max_response_bytes=config.compatibility.max_body_bytes,
                artifact_publisher=artifact_publisher,
            )
        else:
            result[(endpoint.transport, slot_id)] = OpenAIFiniteTransport(
                base_url=endpoint.base_url_identity,
                connection=connection,
                auth_headers=_auth_headers(handle, endpoint.endpoint_id, slot_id, "bearer"),
                max_response_bytes=config.compatibility.max_body_bytes,
                artifact_resolver=artifact_resolver,
                artifact_publisher=artifact_publisher,
            )
    return result


def _auth_headers(
    handle: SecretHandle,
    endpoint_id: str,
    slot_id: str,
    kind: str,
) -> Callable[[], Awaitable[Mapping[str, str]]]:
    async def resolve() -> Mapping[str, str]:
        lease = await handle.acquire()
        material = await lease.resolve()
        try:
            value = material.read_for_wire(CredentialWireAccess(endpoint_id, slot_id))
        finally:
            material.release()
            await lease.release()
        if kind == "anthropic":
            return {"x-api-key": value}
        if kind == "google":
            return {"x-goog-api-key": value}
        return {"authorization": f"Bearer {value}"}

    return resolve


def _aws_credentials(
    handle: SecretHandle,
    endpoint_id: str,
    slot_id: str,
) -> Callable[[], Awaitable[AwsCredentials]]:
    async def resolve() -> AwsCredentials:
        lease = await handle.acquire()
        material = await lease.resolve()
        try:
            raw = material.read_for_wire(CredentialWireAccess(endpoint_id, slot_id))
            value = json.loads(raw)
        finally:
            material.release()
            await lease.release()
        if not isinstance(value, dict):
            raise ValueError("Bedrock credential must be a JSON object")
        return AwsCredentials(
            access_key_id=str(value.get("access_key_id", "")),
            secret_access_key=str(value.get("secret_access_key", "")),
            session_token=(str(value["session_token"]) if value.get("session_token") else None),
        )

    return resolve


def _generation_artifact(
    generation_id: str,
    artifact_digest: str,
    topology_revision: str,
    compiled: CompiledModelGeneration,
    *,
    deployment: str = "embedded",
    activate_immediately: bool = False,
    service_configured: bool = False,
    session_configured: bool = False,
    transfer_configured: bool = False,
) -> GenerationArtifact:
    return GenerationArtifact(
        generation_id=generation_id,
        model_planner_and_bindings={"topology_revision": topology_revision},
        service_planner_and_bindings={
            "runtime": (
                "shared_rpc" if deployment == "shared_process" else "embedded" if service_configured else "unavailable"
            ),
            "configured": deployment == "shared_process" or service_configured,
        },
        session_capability_and_bindings={
            "runtime": (
                "shared_rpc" if deployment == "shared_process" else "embedded" if session_configured else "unavailable"
            ),
            "configured": deployment == "shared_process" or session_configured,
        },
        transfer_capability_and_bindings={
            "runtime": (
                "shared_rpc" if deployment == "shared_process" else "embedded" if transfer_configured else "unavailable"
            ),
            "configured": deployment == "shared_process" or transfer_configured,
        },
        credential_versions={slot: handle.epoch.value for slot, handle in compiled.credential_bindings.handles.items()},
        transport_registry_revision="product-transports-v1",
        client_profile_revision="canonical-v1",
        failure_policy_revision="failure-v2",
        capability_catalog_pricing_snapshot={},
        governance_cache_plugin_revisions={},
        required_wire_contract_range=(3, 2),
        activation_policy={
            "deployment": deployment,
            "activate_immediately": activate_immediately,
        },
        min_reader_version=2,
        min_writer_version=3,
        persistence_schema_versions={"receipt": 1, "usage": 1},
        migration_set_digest="sha256:" + hashlib.sha256(b"embedded-v1").hexdigest(),
        artifact_digest=artifact_digest,
        signer_key_id="embedded-process",
        signature="process-local-capability",
    )


def _shared_application_identity(state_root: Path) -> tuple[str, str, bytes]:
    identity_path = state_root / "inference" / "shared-application-key.json"
    identity_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(identity_path.parent, 0o700)
    try:
        payload = json.loads(identity_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        payload = {
            "application_id": "mote-application",
            "key_id": f"application:{secrets.token_hex(8)}",
            "key": secrets.token_hex(32),
        }
        try:
            descriptor = os.open(
                identity_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            payload = json.loads(identity_path.read_text(encoding="utf-8"))
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
    info = identity_path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError("Shared application identity permissions are unsafe")
    application_id = payload.get("application_id")
    key_id = payload.get("key_id")
    encoded_key = payload.get("key")
    if (
        not isinstance(application_id, str)
        or not application_id
        or not isinstance(key_id, str)
        or not key_id
        or not isinstance(encoded_key, str)
        or not encoded_key
    ):
        raise ValueError("Shared application identity is malformed")
    try:
        key = bytes.fromhex(encoded_key)
    except ValueError as exc:
        raise ValueError("Shared application identity key is malformed") from exc
    if len(key) != 32:
        raise ValueError("Shared application identity key has invalid length")
    return application_id, key_id, key


__all__ = [
    "build_embedded_model_runtime_generation",
    "build_model_runtime_generation",
]
