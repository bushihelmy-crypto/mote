import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from mote.contracts.inference.attempt import InferenceAttemptRequest
from mote.contracts.inference.deadline import CrossProcessDeadline
from mote.contracts.inference.events import AttemptEventType, AttemptLifecycleEvent
from mote.contracts.inference.identity import InferencePrincipal, TrustedSchedulingClass
from mote.contracts.model.failover import EndpointDescriptor
from mote.product.inference.daemon.shared_runtime import SharedInferenceRuntime

DIGEST = "sha256:" + "a" * 64


class Client:
    def envelope(self, **values):
        return values

    async def start_unary(self, request, *, timeout=None):
        assert request.execution_id == "attempt"
        return SimpleNamespace(execution_id="attempt", receipt_revision=1)

    async def resume_events(self, execution_id, **values):
        event = AttemptLifecycleEvent(
            attempt_id=execution_id,
            sequence=1,
            receipt_revision=1,
            generation_id="generation",
            event_type=AttemptEventType.FAILED,
            payload={"reason": "expected"},
        )
        yield SimpleNamespace(
            execution_id=execution_id,
            sequence=1,
            receipt_revision=1,
            event_type="failed",
            payload=event.model_dump_json().encode(),
        )

    async def close(self):
        return None


def test_shared_runtime_preserves_canonical_lifecycle_event():
    async def scenario():
        now = datetime.now(timezone.utc)
        request = InferenceAttemptRequest(
            model_call_id="call",
            owner_journal_id="journal",
            attempt_id="attempt",
            generation_id="generation",
            generation_artifact_digest=DIGEST,
            endpoint=EndpointDescriptor(
                endpoint_id="endpoint",
                transport="openai",
                provider="openai",
                model="model",
                base_url_identity="https://example.test",
                credential_pool_id="pool",
                lifecycle_revision="one",
            ),
            credential_slot_id="slot",
            credential_version="one",
            invocation={"operation": "generate"},
            deadline=CrossProcessDeadline(
                deadline_utc=now + timedelta(seconds=10),
                remaining_seconds_at_send=10,
                sent_at_utc=now,
            ),
            stream=False,
            principal=InferencePrincipal(
                tenant_id="tenant",
                project_id="project",
                subject_id="subject",
                policy_revision="one",
                delegation_digest=DIGEST,
            ),
            scheduling=TrustedSchedulingClass(),
        )
        runtime = SharedInferenceRuntime(Client())
        execution = await runtime.start_attempt(request)
        event = await anext(execution)
        assert event.event_type is AttemptEventType.FAILED
        assert event.payload == {"reason": "expected"}

    asyncio.run(scenario())


def test_non_owning_shared_runtime_does_not_close_generation_client():
    class Client:
        def __init__(self):
            self.closed = 0

        async def close(self):
            self.closed += 1

    async def scenario():
        client = Client()
        runtime = SharedInferenceRuntime(client, owns_client=False)
        await runtime.aclose()
        assert client.closed == 0
        owner = SharedInferenceRuntime(client)
        await owner.aclose()
        assert client.closed == 1

    asyncio.run(scenario())
