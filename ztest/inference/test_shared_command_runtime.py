import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from mote.contracts.inference.deadline import CrossProcessDeadline
from mote.contracts.inference.events import AttemptEventType, AttemptLifecycleEvent
from mote.contracts.inference.executions import BoundExecutionRequest, TransferPartRequest
from mote.contracts.inference.identity import InferencePrincipal, TrustedSchedulingClass
from mote.product.inference.daemon.shared_runtime import SharedArtifactTransferRuntime, SharedServiceCommandRuntime

DIGEST = "sha256:" + "1" * 64


class Client:
    def __init__(self):
        self.starts = []

    def envelope(self, **fields):
        return fields

    async def start_durable_command(self, request, *, timeout=None):
        self.starts.append(("command", request))
        return type("Response", (), {"execution_id": request.execution_id, "receipt_revision": 1})()

    async def execute_transfer_part(self, request, *, timeout=None):
        self.starts.append(("transfer", request))
        return type("Response", (), {"execution_id": request.start.execution_id, "receipt_revision": 1})()

    async def resume_events(self, execution_id, **kwargs):
        event = AttemptLifecycleEvent(
            attempt_id=execution_id,
            sequence=1,
            receipt_revision=2,
            generation_id="generation",
            event_type=AttemptEventType.SUCCEEDED,
        )
        yield type(
            "Raw",
            (),
            {
                "execution_id": execution_id,
                "sequence": 1,
                "receipt_revision": 2,
                "event_type": "succeeded",
                "payload": event.model_dump_json().encode(),
            },
        )()

    async def close(self):
        return None


def _base(cls=BoundExecutionRequest, **extra):
    now = datetime.now(timezone.utc)
    return cls(
        execution_id=extra.pop("execution_id", "command"),
        owner_journal_id="journal",
        generation_id="generation",
        generation_artifact_digest=DIGEST,
        endpoint_binding_id="binding",
        credential_slot_id="slot",
        credential_version="1",
        operation="operation",
        payload={},
        deadline=CrossProcessDeadline(
            deadline_utc=now + timedelta(seconds=5),
            remaining_seconds_at_send=5,
            sent_at_utc=now,
        ),
        principal=InferencePrincipal(
            tenant_id="tenant",
            project_id="project",
            subject_id="subject",
            policy_revision="policy",
            delegation_digest=DIGEST,
        ),
        scheduling=TrustedSchedulingClass(),
        **extra,
    )


def test_shared_command_and_transfer_use_distinct_rpc_contracts():
    async def scenario():
        client = Client()
        command_runtime = SharedServiceCommandRuntime(client)
        command = await command_runtime.start_command(_base())
        assert (await anext(command)).event_type is AttemptEventType.SUCCEEDED

        transfer_runtime = SharedArtifactTransferRuntime(client)
        transfer = await transfer_runtime.execute_part(
            _base(
                TransferPartRequest,
                execution_id="transfer",
                transfer_id="artifact",
                part_number=1,
                offset=0,
                length=4,
                content_digest=DIGEST,
            )
        )
        assert (await anext(transfer)).event_type is AttemptEventType.SUCCEEDED
        await command_runtime.drain(timeout_seconds=1)
        await transfer_runtime.drain(timeout_seconds=1)
        return client.starts

    starts = asyncio.run(scenario())
    assert [kind for kind, _request in starts] == ["command", "transfer"]
    assert starts[1][1].part_number == 1
    assert starts[1][1].content_digest == DIGEST


def test_shared_command_drain_rejects_new_work():
    async def scenario():
        client = Client()
        runtime = SharedServiceCommandRuntime(client)
        execution = await runtime.start_command(_base())
        draining = asyncio.create_task(runtime.drain(timeout_seconds=1))
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="draining"):
            await runtime.start_command(_base(execution_id="second"))
        assert (await anext(execution)).terminal
        await draining

    asyncio.run(scenario())
