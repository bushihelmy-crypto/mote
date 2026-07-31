import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import grpc
import pytest

from mote.contracts.inference.deadline import CrossProcessDeadline
from mote.contracts.inference.identity import InferencePrincipal
from mote.contracts.inference.shared import SharedHandshake
from mote.product.inference.daemon.application import SharedDaemonApplication
from mote.product.inference.daemon.grpc_client import SharedGrpcClient
from mote.product.inference.daemon.security import sign_handshake
from mote.product.inference.daemon.shared_runtime import SharedInferenceRuntime
from mote.runtime.models.inference_attempt_executor import InferenceAttemptExecutor
from mote.ztest.inference.test_embedded_runtime import FakeGenerateTransport, Resolver, _request
from mote.ztest.inference.test_shared_grpc import _artifact, _handshake


class UnusedResolvers:
    def resolve_generate(self, request):
        raise AssertionError("not used")

    def resolve_command(self, request):
        raise AssertionError("not used")

    def resolve_session(self, request):
        raise AssertionError("not used")

    def resolve_transfer_part(self, request):
        raise AssertionError("not used")


def test_shared_daemon_application_stages_generation_and_opens_admission(tmp_path):
    async def scenario():
        socket_path = tmp_path / "gateway.sock"
        resolvers = UnusedResolvers()
        application = SharedDaemonApplication(
            socket_path=socket_path,
            database_path=tmp_path / "authority.sqlite3",
            socket_generation="socket-generation",
            application_keys={"application": ("application-key", b"application-secret")},
            session_key_id="session-key",
            session_key=b"session-secret",
            protocol_version=3,
            tenant_id="tenant",
            project_id="project",
            generate_transports=Resolver(
                FakeGenerateTransport(
                    payload={
                        "output": {"kind": "generate", "content": "ok"},
                        "usage": {
                            "input_tokens": 1,
                            "output_tokens": 1,
                            "total_tokens": 2,
                        },
                    },
                    usage_units=2,
                )
            ),
            command_transports=resolvers,
            session_transports=resolvers,
            transfer_transports=resolvers,
            worker_count=1,
            global_in_flight=1,
            provider_in_flight=1,
            endpoint_in_flight=1,
        )
        await application.start()
        client = SharedGrpcClient(socket_path)
        try:
            assert application.readiness[0] is False
            await client.authenticate(_handshake())
            artifact = _artifact()
            status = await client.stage_generation(
                artifact.model_dump_json().encode(),
                generation_id=artifact.generation_id,
                artifact_digest=artifact.artifact_digest,
            )
            assert status.state == "active"
            assert application.readiness[0] is True
            assert (socket_path.stat().st_mode & 0o777) == 0o600
            assert os.stat(tmp_path / "authority.sqlite3").st_mode & 0o777 == 0o600

            now = datetime.now(timezone.utc)
            canonical = _request().model_copy(
                update={
                    "generation_id": artifact.generation_id,
                    "generation_artifact_digest": artifact.artifact_digest,
                    "deadline": CrossProcessDeadline(
                        deadline_utc=now + timedelta(seconds=5),
                        remaining_seconds_at_send=5,
                        sent_at_utc=now,
                    ),
                    "principal": InferencePrincipal(
                        tenant_id="tenant",
                        project_id="project",
                        subject_id="subject",
                        policy_revision="policy-1",
                        delegation_digest="sha256:" + "8" * 64,
                    ),
                }
            )
            executor = InferenceAttemptExecutor(
                SharedInferenceRuntime(client),
                client.permit_issuer(),
                permit_audience="shared/socket-generation/model/tenant",
                epoch_provider=lambda: (0, 0),
            )
            authorizations = []

            async def append(record):
                authorizations.append(record)

            result = await executor.execute(
                canonical,
                ordinal=1,
                resume_generation=0,
                issued_journal_revision=1,
                append_authorization=append,
            )
            assert result.response.output.content == "ok"
            assert len(authorizations) == 1

            backup = tmp_path / "backup.sqlite3"
            operations_client = SharedGrpcClient(socket_path)
            with pytest.raises(grpc.aio.AioRpcError) as denied:
                await client.backup(backup, consistency="crash_consistent")
            assert denied.value.code() is grpc.StatusCode.PERMISSION_DENIED
            unsigned = _handshake().model_copy(
                update={
                    "project_id": "gateway-operations",
                    "subject_id": "gateway-cli",
                    "policy_revision": "gateway-operations-v1",
                    "nonce": os.urandom(16).hex(),
                    "signature": "unsigned",
                }
            )
            await operations_client.authenticate(
                sign_handshake(SharedHandshake.model_validate(unsigned), b"application-secret")
            )
            created = await operations_client.backup(backup, consistency="crash_consistent")
            assert created.consistency == "crash_consistent"
            assert created.digest.startswith("sha256:")
            verified = await operations_client.verify_restore(backup)
            assert verified.verified is True
            assert verified.digest == created.digest
            reconciled = await operations_client.reconcile_all()
            assert reconciled.attempts == 0
            drained = await operations_client.begin_drain(timeout_seconds=0.1)
            assert drained.ready is False
            assert drained.components["admission"] == "closed"
            audit_path = tmp_path / "authority.sqlite3.operations-audit.jsonl"
            audit_records = [json.loads(line) for line in audit_path.read_text().splitlines()]
            assert [record["envelope"]["payload"]["operation"] for record in audit_records] == [
                "backup",
                "reconcile_all",
                "begin_drain",
            ]
            await operations_client.close()
        finally:
            await client.close()
            await application.drain_and_stop(timeout_seconds=0.1)

    asyncio.run(scenario())
