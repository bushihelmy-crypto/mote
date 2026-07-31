"""Production composition root for one same-user Shared inference daemon."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from mote.contracts.inference.backup import BackupConsistency
from mote.contracts.ports.inference.command_transport import BoundCommandTransportResolver
from mote.contracts.ports.inference.provider_transport import GenerateTransportResolver
from mote.contracts.ports.inference.session_transport import SessionTransportResolver
from mote.contracts.ports.inference.transfer_transport import ProviderTransferPartTransportResolver
from mote.product.inference.backends.sqlite import (
    SQLiteAttemptReceiptStore,
    SQLiteReconciliationAuthority,
    SQLiteSessionReceiptStore,
    SQLiteUsageLedger,
)
from mote.product.inference.daemon.admin_projection import build_daemon_admin_read_model
from mote.product.inference.daemon.execution_backend import SharedEmbeddedExecutionBackend
from mote.product.inference.daemon.generation import SharedGenerationBackend
from mote.product.inference.daemon.grpc_server import SharedGrpcServer
from mote.product.inference.daemon.lifecycle import SharedDaemonLifecycle
from mote.product.inference.daemon.operations_audit import SharedOperationsAudit
from mote.product.inference.daemon.security import SharedHandshakeAuthority
from mote.product.inference.security.wire_permit import Ed25519WirePermitVerifier
from mote.runtime.inference.command_runtime import EmbeddedServiceCommandRuntime
from mote.runtime.inference.generation import GatewayGenerationOwner
from mote.runtime.inference.governance import CredentialHealthAuthority, ProviderQuotaAuthority
from mote.runtime.inference.runtime import EmbeddedInferenceRuntime
from mote.runtime.inference.session_runtime import EmbeddedSessionRuntime
from mote.runtime.inference.transfer_runtime import EmbeddedArtifactTransferRuntime


class SharedDaemonApplication:
    def __init__(
        self,
        *,
        socket_path: Path,
        database_path: Path,
        socket_generation: str,
        application_keys: Mapping[str, tuple[str, bytes]],
        session_key_id: str,
        session_key: bytes,
        protocol_version: int,
        tenant_id: str,
        project_id: str,
        generate_transports: GenerateTransportResolver,
        command_transports: BoundCommandTransportResolver,
        session_transports: SessionTransportResolver,
        transfer_transports: ProviderTransferPartTransportResolver,
        busy_timeout_seconds: float = 5.0,
        hard_min_free_bytes: int = 0,
        queue_capacity: int = 5000,
        event_capacity: int = 256,
        worker_count: int = 16,
        global_in_flight: int = 1000,
        provider_in_flight: int = 100,
        endpoint_in_flight: int = 100,
    ) -> None:
        if not socket_path.is_absolute() or not database_path.is_absolute():
            raise ValueError("Shared daemon paths must be absolute")
        if not tenant_id or not project_id:
            raise ValueError("Shared daemon tenant and project identity are required")
        receipts = SQLiteAttemptReceiptStore(
            database_path,
            busy_timeout_seconds=busy_timeout_seconds,
        )
        session_receipts = SQLiteSessionReceiptStore(receipts)
        usage = SQLiteUsageLedger(receipts)
        reconciliation = SQLiteReconciliationAuthority(receipts)
        generations = GatewayGenerationOwner()
        verifier = Ed25519WirePermitVerifier({})
        quota = ProviderQuotaAuthority()
        health = CredentialHealthAuthority()
        epoch = lambda: (0, 0)
        audience = f"shared/{socket_generation}/model/{tenant_id}"
        runtime_limits = {
            "queue_capacity": queue_capacity,
            "event_capacity": event_capacity,
            "worker_count": worker_count,
            "global_in_flight": global_in_flight,
            "provider_in_flight": provider_in_flight,
            "endpoint_in_flight": endpoint_in_flight,
        }
        unary = EmbeddedInferenceRuntime(
            receipts=receipts,
            usage_ledger=usage,
            reserve_units=lambda request: max(request.endpoint.execution_policy.max_output_tokens, 1),
            provider_quota=quota,
            credential_health=health,
            permit_verifier=verifier,
            transports=generate_transports,
            generations=generations,
            permit_audience=audience,
            epoch_provider=epoch,
            **runtime_limits,
        )
        commands = EmbeddedServiceCommandRuntime(
            receipts=receipts,
            usage_ledger=usage,
            reserve_units=lambda _request: 1,
            provider_quota=quota,
            credential_health=health,
            permit_verifier=verifier,
            transports=command_transports,
            generations=generations,
            permit_audience=audience,
            epoch_provider=epoch,
            **runtime_limits,
        )
        sessions = EmbeddedSessionRuntime(
            session_receipts=session_receipts,
            wire_receipts=receipts,
            usage_ledger=usage,
            reserve_open_units=lambda _request: 1,
            reserve_message_units=lambda _message: 1,
            provider_quota=quota,
            credential_health=health,
            permit_verifier=verifier,
            transports=session_transports,
            generations=generations,
            permit_audience=audience,
            epoch_provider=epoch,
            **runtime_limits,
        )
        transfers = EmbeddedArtifactTransferRuntime(
            receipts=receipts,
            usage_ledger=usage,
            reserve_units=lambda _request: 1,
            provider_quota=quota,
            credential_health=health,
            permit_verifier=verifier,
            transports=transfer_transports,
            generations=generations,
            permit_audience=audience,
            epoch_provider=epoch,
            **runtime_limits,
        )
        lifecycle = SharedDaemonLifecycle(
            persistence=receipts,
            generations=generations,
            hard_min_free_bytes=hard_min_free_bytes,
        )
        backend = SharedEmbeddedExecutionBackend(
            unary=unary,
            commands=commands,
            sessions=sessions,
            transfers=transfers,
            receipts=receipts,
            session_receipts=session_receipts,
            events=receipts,
        )
        authority = SharedHandshakeAuthority(
            socket_generation=socket_generation,
            application_keys=application_keys,
            session_key_id=session_key_id,
            session_key=session_key,
            current_protocol_version=protocol_version,
            permit_verifier=verifier,
        )
        generations_backend = SharedGenerationBackend(
            generations,
            persistence=receipts,
            on_activation=lifecycle.open_admission_after_generation_activation,
        )
        self._tenant_id = tenant_id
        self._project_id = project_id
        self._usage = usage
        self._receipts = receipts
        self._reconciliation = reconciliation
        self._operations_audit = SharedOperationsAudit(
            database_path.with_suffix(database_path.suffix + ".operations-audit.jsonl")
        )
        self._lifecycle = lifecycle
        self._backend = backend
        self._generations_backend = generations_backend
        self._server = SharedGrpcServer(
            socket_path=socket_path,
            authority=authority,
            backend=backend,
            generations=generations_backend,
            readiness=lifecycle.readiness,
            operations=self,
        )
        self._started = False

    @property
    def readiness(self) -> tuple[bool, Mapping[str, str]]:
        return self._lifecycle.readiness()

    def admin_read_model(self):
        return build_daemon_admin_read_model(
            receipts=self._receipts,
            readiness=self._lifecycle.readiness,
            audit=self._operations_audit,
            reconciliation_authority=self._reconciliation,
        )

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("Shared daemon application is already started")
        await self._lifecycle.start()
        await self._usage.configure_budget(
            self._tenant_id,
            self._project_id,
            (1 << 62) - 1,
        )
        await self._server.start()
        self._started = True

    async def drain_and_stop(self, *, timeout_seconds: float) -> None:
        if not self._started:
            return
        self._lifecycle.begin_drain()
        await self._backend.drain(timeout_seconds=timeout_seconds)
        await self._backend.aclose()
        await self._server.stop(grace_seconds=timeout_seconds)
        self._started = False

    async def backup(self, destination: Path, consistency: str) -> str:
        if consistency != BackupConsistency.CRASH_CONSISTENT.value:
            raise ValueError("Shared daemon online backup consistency must be crash_consistent")
        if not destination.is_absolute():
            raise ValueError("backup destination must be absolute")
        await self._receipts.backup_to(destination)
        metadata = await self._receipts.describe_backup(destination)
        await self._operations_audit.record(
            "backup",
            "committed",
            consistency=consistency,
            digest=metadata.authority_digest,
            logical_store=metadata.logical_store,
            cutover_unit_id=metadata.cutover_unit_id,
            source_generation=metadata.source_generation,
            storage_format_version=metadata.storage_format_version,
            high_water_mark=metadata.high_water_mark,
        )
        return metadata.authority_digest

    async def verify_restore(self, source: Path) -> str:
        if not source.is_absolute():
            raise ValueError("restore source must be absolute")
        return await self._receipts.verify_backup(source)

    async def reconcile_all(self) -> tuple[int, int]:
        attempts, unresolved = await self._receipts.reconcile_incomplete()
        await self._operations_audit.record(
            "reconcile_all",
            "committed",
            attempts=attempts,
            unresolved=unresolved,
        )
        return attempts, unresolved

    async def begin_drain(self, *, timeout_seconds: float) -> None:
        self._lifecycle.begin_drain()
        await self._backend.drain(timeout_seconds=timeout_seconds)
        self._lifecycle.finish_drain()
        await self._operations_audit.record("begin_drain", "committed", timeout_seconds=timeout_seconds)

    async def observe_generation(self, request, credential):
        return await self._generations_backend.observe_generation(request, credential)

    async def wait(self) -> None:
        await self._server.wait()


__all__ = ["SharedDaemonApplication"]
