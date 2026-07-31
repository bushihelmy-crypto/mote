import asyncio
from datetime import datetime, timedelta, timezone

from mote.contracts.inference.deadline import CrossProcessDeadline
from mote.contracts.inference.events import AttemptEventType, AttemptLifecycleEvent
from mote.contracts.inference.executions import BoundExecutionRequest, TransferPartRequest
from mote.contracts.inference.identity import InferencePrincipal, TrustedSchedulingClass
from mote.contracts.inference.wire_permit import WirePermit
from mote.product.inference.command_gateway import RuntimeArtifactTransferGateway, RuntimeCommandGateway

DIGEST = "sha256:" + "a" * 64


def _base(operation, payload):
    now = datetime.now(timezone.utc)
    return BoundExecutionRequest(
        execution_id=f"execution-{operation}",
        owner_journal_id="journal",
        generation_id="generation",
        generation_artifact_digest=DIGEST,
        endpoint_binding_id="binding",
        credential_slot_id="slot",
        credential_version="1",
        operation=operation,
        payload=payload,
        deadline=CrossProcessDeadline(
            deadline_utc=now + timedelta(minutes=1),
            remaining_seconds_at_send=60,
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
    )


def _transfer(operation, payload):
    return TransferPartRequest(
        **_base(operation, payload).model_dump(),
        transfer_id="transfer",
        part_number=1,
        offset=0,
        length=4,
        content_digest="sha256:" + "b" * 64,
    )


class _Issuer:
    def __init__(self):
        self.calls = []

    def issue(self, **values):
        self.calls.append(values)
        return WirePermit(
            nonce="0123456789abcdef",
            issuer_key_id="key",
            trust_revision=1,
            signature="signature",
            **values,
        )


class _Execution:
    def __init__(self, request, result):
        self.request = request
        self.result = result
        self.events = [
            AttemptLifecycleEvent(
                attempt_id=request.execution_id,
                sequence=1,
                receipt_revision=1,
                generation_id=request.generation_id,
                event_type=AttemptEventType.QUEUED,
            ),
            AttemptLifecycleEvent(
                attempt_id=request.execution_id,
                sequence=2,
                receipt_revision=4,
                generation_id=request.generation_id,
                event_type=AttemptEventType.WIRE_AUTHORIZATION_REQUIRED,
            ),
        ]
        self.permits = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.events:
            raise StopAsyncIteration
        return self.events.pop(0)

    async def authorize_wire(self, permit):
        self.permits.append(permit)
        self.events.append(
            AttemptLifecycleEvent(
                attempt_id=self.request.execution_id,
                sequence=3,
                receipt_revision=8,
                generation_id=self.request.generation_id,
                event_type=AttemptEventType.SUCCEEDED,
                payload={"result": self.result},
            )
        )

    async def cancel(self, reason):
        return None


class _Runtime:
    def __init__(self, result):
        self.result = result
        self.requests = []

    async def start_command(self, request):
        self.requests.append(request)
        return _Execution(request, self.result)

    async def execute_part(self, request):
        self.requests.append(request)
        return _Execution(request, self.result)


def test_command_and_transfer_gateways_issue_exact_taxonomy_permits():
    async def scenario():
        command_runtime, transfer_runtime = _Runtime({"id": "batch-1"}), _Runtime({"etag": "e"})
        command_issuer, transfer_issuer = _Issuer(), _Issuer()
        command = RuntimeCommandGateway(
            command_runtime,
            command_issuer,
            _base,
            permit_audience="service",
            epoch_provider=lambda: (2, 3),
        )
        transfer = RuntimeArtifactTransferGateway(
            transfer_runtime,
            transfer_issuer,
            _transfer,
            permit_audience="transfer",
            epoch_provider=lambda: (4, 5),
        )
        return (
            await command.execute("batch.create", {"input": "f"}),
            await transfer.execute_part("file.upload", {"artifact": "a"}),
            command_issuer.calls,
            transfer_issuer.calls,
        )

    command_result, transfer_result, commands, transfers = asyncio.run(scenario())
    assert command_result == {"id": "batch-1"}
    assert transfer_result == {"etag": "e"}
    assert commands[0]["execution_taxonomy"] == "durable_operation"
    assert transfers[0]["execution_taxonomy"] == "artifact_transfer"
    assert commands[0]["issued_journal_revision"] == 4
    assert commands[0]["backup_epoch"] == 2
    assert transfers[0]["admission_epoch"] == 5
