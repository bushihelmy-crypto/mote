import asyncio
from datetime import datetime, timedelta, timezone

from mote.contracts.inference.deadline import CrossProcessDeadline
from mote.contracts.inference.executions import TransferPartRequest
from mote.contracts.inference.identity import InferencePrincipal, TrustedSchedulingClass
from mote.contracts.inference.transport import ProviderWireResult
from mote.contracts.inference.wire_permit import WirePermit
from mote.product.inference.backends.sqlite import SQLiteAttemptReceiptStore, SQLiteUsageLedger
from mote.runtime.inference.governance import CredentialHealthAuthority, ProviderQuotaAuthority
from mote.runtime.inference.transfer_runtime import EmbeddedArtifactTransferRuntime

DIGEST = "sha256:" + "2" * 64
CONTENT_DIGEST = "sha256:" + "3" * 64


class _Lease:
    generation_id = "generation"
    artifact_digest = DIGEST

    def __init__(self):
        self.released = False

    def release(self):
        self.released = True


class _Generations:
    def acquire(self, domain):
        return _Lease()


class _Transport:
    provider = "storage-provider"
    endpoint_id = "upload-endpoint"
    wire_protocol = "https"

    def __init__(self):
        self.calls = 0

    async def execute_once(self, request, *, local_deadline, lifecycle):
        self.calls += 1
        await lifecycle.wire_started()
        await lifecycle.response_started()
        return ProviderWireResult(
            payload={"etag": "part-etag", "part_number": request.part_number},
            usage_units=request.length,
        )

    async def aclose(self):
        return None


class _Resolver:
    def __init__(self, transport):
        self.transport = transport

    def resolve_transfer_part(self, request):
        return self.transport


class _Verifier:
    async def verify(self, permit):
        return permit.signature == "embedded"


def _request():
    now = datetime.now(timezone.utc)
    return TransferPartRequest(
        execution_id="transfer-1:part-1",
        owner_journal_id="transfer-journal",
        generation_id="generation",
        generation_artifact_digest=DIGEST,
        endpoint_binding_id="binding",
        credential_slot_id="slot",
        credential_version="1",
        operation="multipart.upload_part",
        payload={"artifact_reference": "artifact://source"},
        deadline=CrossProcessDeadline(
            deadline_utc=now + timedelta(seconds=5),
            remaining_seconds_at_send=5,
            sent_at_utc=now,
        ),
        principal=InferencePrincipal(
            tenant_id="tenant",
            project_id="project",
            subject_id="subject",
            policy_revision="1",
            delegation_digest=DIGEST,
        ),
        scheduling=TrustedSchedulingClass(),
        transfer_id="transfer-1",
        part_number=1,
        offset=0,
        length=8,
        content_digest=CONTENT_DIGEST,
    )


def _permit(request):
    now = datetime.now(timezone.utc)
    return WirePermit(
        attempt_id=request.execution_id,
        execution_taxonomy="artifact_transfer",
        owner_journal_id=request.owner_journal_id,
        wire_unit=request.operation,
        generation_id=request.generation_id,
        generation_artifact_digest=request.generation_artifact_digest,
        ordinal=1,
        nonce="0123456789abcdef",
        issued_journal_revision=1,
        not_before=now,
        expires_at=now + timedelta(minutes=1),
        issuer_key_id="key",
        audience="embedded/transfer/tenant",
        trust_revision=1,
        backup_epoch=0,
        admission_epoch=0,
        signature="embedded",
    )


def test_transfer_runtime_executes_exactly_one_digest_bound_part(tmp_path):
    async def scenario():
        authority = SQLiteAttemptReceiptStore(tmp_path / "gateway.sqlite3")
        await authority.initialize()
        ledger = SQLiteUsageLedger(authority)
        await ledger.configure_budget("tenant", "project", 100)
        transport = _Transport()
        runtime = EmbeddedArtifactTransferRuntime(
            receipts=authority,
            usage_ledger=ledger,
            reserve_units=lambda request: request.length,
            provider_quota=ProviderQuotaAuthority(),
            credential_health=CredentialHealthAuthority(),
            permit_verifier=_Verifier(),
            transports=_Resolver(transport),
            generations=_Generations(),
            permit_audience="embedded/transfer/tenant",
            epoch_provider=lambda: (0, 0),
            worker_count=1,
            global_in_flight=1,
            provider_in_flight=1,
            endpoint_in_flight=1,
        )
        request = _request()
        execution = await runtime.execute_part(request)
        events = []
        async for event in execution:
            events.append(event.event_type.value)
            if event.event_type.value == "wire_authorization_required":
                await execution.authorize_wire(_permit(request))
        await runtime.drain(timeout_seconds=2)
        await runtime.aclose()
        assert transport.calls == 1
        assert events[-1] == "succeeded"

    asyncio.run(scenario())
